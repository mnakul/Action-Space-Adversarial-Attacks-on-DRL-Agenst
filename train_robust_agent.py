from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import absolute_import
from builtins import *  # NOQA
from future import standard_library
standard_library.install_aliases()  # NOQA
import argparse
import os

import gym
import gym.wrappers
import numpy as np
from functools import partial

import chainer
from chainer import functions as F
from chainer import links as L
from chainer import Sequential
import chainerrl
from chainerrl.agents import PPO
from chainerrl import links, distribution, policies, experiments, misc
from chainerrl.optimizers.nonbias_weight_decay import NonbiasWeightDecay
from chainerrl.initializers import LeCunNormal
from chainerrl.policy import Policy

import safety_gym

def main():
	import logging
	parser = argparse.ArgumentParser()
	parser.add_argument('--gpu', type=int, default=-1)
	parser.add_argument('--steps', type=int, default=1000000)
	parser.add_argument('--env', type=str, default= 'PointGoal2.0-v1', choices=('PointGoal2.0-v1', 'CarGoal2.0-v1')) 
	parser.add_argument('--bound-mean', type=bool, default=True)
	parser.add_argument('--seed', type=int, default=0,
						help='Random seed [0, 2 ** 32)')
	parser.add_argument('--reward-scale-factor', type=float, default=1e-2)
	parser.add_argument('--standardize-advantages', action='store_true')
	parser.add_argument('--render', default=False)
	parser.add_argument('--lr', type=float, default=3e-4)
	parser.add_argument('--update-interval', type=int, default=2048)
	parser.add_argument('--epochs', type=int, default=10)
	parser.add_argument('--entropy-coef', type=float, default=0.0)
	parser.add_argument('--weight-decay', type=float, default=0)
	parser.add_argument('--batchsize', type=int, default=1024)
	parser.add_argument('--logger-level', type=int, default=logging.DEBUG)
	parser.add_argument('--monitor', action='store_true')
	parser.add_argument('--variant', type=int, default=2, choices=(1,2,3),
						help='Specify which variant of adversartial training. 1:(Untrained agent & adv). 2: (Untrained agent & trained adv). 3:(Trained agent & adv)')
	parser.add_argument('--save_dir', type=str, default='robust',
						help='Directory to save adv. training results results') 
	parser.add_argument('--goals_norm', type=float, default=1.0, 
						help='Minimum distance adversarial goal should be generated from agent initial position')
	parser.add_argument('--load', type=str, default='nominal/CarGoal2.0-v1_0',
						help='Directory to load trained agent')
	parser.add_argument('--adv_load', type=str, default='adversary/weights/CarGoal2.0-v1_SA_0',
						help='Directory to load trained adversary agent')
	args = parser.parse_args()
	logging.basicConfig(level=args.logger_level)
	misc.set_random_seed(args.seed, gpus=(args.gpu,))

	#duplicate safety-gym configs but extend gym dimensions and simplify lidar to cover entire space
	if args.env=='PointGoal2.0-v1':
		env = gym.make('Safexp-PointGoal0-v0')
	elif args.env=='CarGoal2.0-v1':
		env = gym.make('Safexp-CarGoal0-v0')
	config = env.config
	config['placements_extents']= [-2.0, -2.0, 2.0, 2.0]
	config['lidar_max_dist'] = 8*config['placements_extents'][3]
	from safety_gym.envs.engine import Engine
	env = Engine(config)
	from gym.envs.registration import register
	register(id=args.env,
	         entry_point='safety_gym.envs.mujoco:Engine',
	         kwargs={'config': config})

	def make_env(args_env,test):
		env = gym.make(args.env)
		# Use different random seeds for train and test envs
		env_seed = 2 ** 32 - 1 - args.seed if test else args.seed
		env.seed(env_seed)
		# Cast observations to float32 because our model uses float32
		env = chainerrl.wrappers.CastObservationToFloat32(env)
		if args.monitor:
			env = gym.wrappers.Monitor(env, args.outdir)
		if not test:
			# Scale rewards (and thus returns) to a reasonable range so that training is easier
			env = chainerrl.wrappers.ScaleReward(env, args.reward_scale_factor)
		return env

	env = make_env(args.env, test=False)
	obs_space = env.observation_space
	action_space = env.action_space
	action_size = action_space.low.size

	# Normalize observations based on their empirical mean and variance
	obs_normalizer = chainerrl.links.EmpiricalNormalization(obs_space.low.size, clip_threshold=5)

	# as per paper, we only consider StateUnaware (A) variant of adversarial policy for 
	# adversarial training since that is more effective than StateAware (SA) variant
	adv_obs_normalizer = chainerrl.links.EmpiricalNormalization(
		action_space.low.size + 16, clip_threshold=5)

	winit = chainerrl.initializers.Orthogonal(1.)
	winit_last = chainerrl.initializers.Orthogonal(1e-2)
	policy = chainer.Sequential(
				L.Linear(None, 64, initialW=winit), 
				F.tanh,
				L.Linear(None, 64, initialW=winit),
				F.tanh,
				L.Linear(None, action_size, initialW=winit_last),
				chainerrl.policies.GaussianHeadWithStateIndependentCovariance(
					action_size=action_size,
					var_type='diagonal',
					var_func=lambda x: F.exp(2 * x),  # Parameterize log std
					var_param_init=0,  # log std = 0 => std = 1
					),
				)

	vf = chainer.Sequential(
				L.Linear(None, 128, initialW=winit),
				F.tanh,
				L.Linear(None, 64, initialW=winit),
				F.tanh,
				L.Linear(None, 1, initialW=winit),
				)

	adv_policy = chainer.Sequential(
				L.Linear(None, 128, initialW=winit), 
				F.tanh,
				L.Linear(None, 128, initialW=winit),
				F.tanh,
				L.Linear(None, action_size, initialW=winit_last),
				chainerrl.policies.GaussianHeadWithStateIndependentCovariance(
					action_size=action_size,
					var_type='diagonal',
					var_func=lambda x: F.exp(2 * x),  # Parameterize log std
					var_param_init=0,  # log std = 0 => std = 1
					),
				)

	adv_vf = chainer.Sequential(
				L.Linear(None, 256, initialW=winit),
				F.tanh,
				L.Linear(None, 64, initialW=winit),
				F.tanh,
				L.Linear(None, 1, initialW=winit),
				)
	
	model = chainerrl.links.Branched(policy, vf)
	adv_model = chainerrl.links.Branched(adv_policy, adv_vf)

	if args.gpu > -1:
		import cupy as cp
		model.to_gpu(args.gpu)
		adv_model.to_gpu(args.gpu)

	opt = chainer.optimizers.Adam(alpha=args.lr, eps=1e-5)
	adv_opt = chainer.optimizers.Adam(alpha=args.lr, eps=1e-5)
	opt.setup(model)
	adv_opt.setup(adv_model)

	if args.weight_decay > 0:
		opt.add_hook(NonbiasWeightDecay(args.weight_decay))

	agent = PPO(model, opt,
		obs_normalizer=obs_normalizer, gpu=args.gpu,
		update_interval=args.update_interval, 
		minibatch_size=args.batchsize, epochs=args.epochs,
		clip_eps_vf=None, entropy_coef=args.entropy_coef,
		standardize_advantages=args.standardize_advantages,
		gamma=0.995,
		lambd=0.97,
		)

	adversary = PPO(adv_model, adv_opt,
		obs_normalizer=adv_obs_normalizer, gpu=args.gpu,
		update_interval=args.update_interval, 
		minibatch_size=args.batchsize, epochs=args.epochs,
		clip_eps_vf=None, entropy_coef=args.entropy_coef,
		standardize_advantages=args.standardize_advantages,
		gamma=0.995,
		lambd=0.97,
		)

	# variant 1: untrained policies, 2: trained adv, 3: trained policies
	if args.variant == 2:	
		adversary.load(args.adv_load)
	elif args.variant == 3:
		adversary.load(args.adv_load)
		agent.load(args.load)

	COLOR_BUTTON = np.array([0.8, .5, 0.2, 1])

	#initialize list to log statistics
	adv_Rs = []
	env_Rs = []

	i = 0
	if args.variant == 1:
		print('Adversarially training nominal and adversarial policy from scratch')
		while i < args.steps:
			#reset to generate random adversarial goal
			_ = env.reset()
			adv_goal = env.goal_pos
			#sample new nominal goal
			env_obs = env.reset()
			last_dist_adv_goal = env.dist_xy(adv_goal)
			last_dist_nom_goal = env.dist_xy(env.goal_pos)
			done = False
			t = 0.0
			env_R = 0.0
			adv_R = 0.0
			env_r = 0.0
			adv_r = 0.0
			while not done and t < 1000:
				action = agent.act_and_train(env_obs, env_r)
				lidar_to_adv = env.obs_lidar([adv_goal],0)
				concat_obs = np.hstack((action, lidar_to_adv)).astype('float32')
				delta = adversary.act_and_train(concat_obs, adv_r)
				adv_action = action + delta
				env_obs, env_r, done, _ = env.step(adv_action)

				# env.render()
				# env.render_area(adv_goal , 0.3, COLOR_BUTTON, 'adv_goal', alpha=0.5)

				dist_adv_goal = env.dist_xy(adv_goal)
				# penalty for adversary entering nominal goal
				goal_penalty = 0
				if env.dist_xy(env.goal_pos) <= 0.3:
					goal_penalty = -1
					if env.dist_xy(adv_goal) <= 0.3:
						goal_penalty = 0

				adv_r = (last_dist_adv_goal - dist_adv_goal)*1 - (last_dist_nom_goal-env.dist_xy(env.goal_pos))

				#manual scaling of rewards since env rewards not going through wrapper
				adv_r = adv_r*1e-2
				adv_r = adv_r.astype('float32')
				last_dist_adv_goal = dist_adv_goal
				last_dist_nom_goal = env.dist_xy(env.goal_pos)

				#re-sample new pair of goals if nominal goal reached
				if  env.dist_xy(env.goal_pos) <= 0.35:
					_ = env.reset()
					adv_goal = env.goal_pos
					env_obs = env.reset()
					last_dist_adv_goal = env.dist_xy(adv_goal)

				#re-sample new pair of goals if adversarial goal reached
				if  env.dist_xy(adv_goal) <= 0.3:
					adv_r += (1*1e-2)
					_ = env.reset()
					adv_goal = env.goal_pos
					env_obs = env.reset()
					last_dist_adv_goal = env.dist_xy(adv_goal)

				env_R += env_r
				adv_R += adv_r
				t += 1
				i += 1
			 
			adv_Rs.append(adv_R)
			env_Rs.append(env_R)

			if i % 1000 == 0:
				print('Step:', i, 'R:', env_R, 'adv_R:', adv_R)
				print('')
				print('statistics:', agent.get_statistics())
			agent.stop_episode_and_train(env_obs, env_r, done)
			adversary.stop_episode_and_train(concat_obs, adv_r, done)
	elif args.variant == 2 or args.variant == 3:
		if args.variant == 2:
			print('Adversarially training nominal policy from scratch')
		else:
			print('Adversarially training nominal policy with pre-trained weights')
		while i < args.steps:
			#reset to generate random adversarial goal
			_ = env.reset()
			adv_goal = env.goal_pos
			#sample new nominal goal
			env_obs = env.reset()
			last_dist_adv_goal = env.dist_xy(adv_goal)
			done = False
			t = 0.0
			env_R = 0.0
			adv_R = 0.0
			env_r = 0.0
			adv_r = 0.0
			while not done and t < 1000:
				action = agent.act_and_train(env_obs, env_r)
				lidar_to_adv = env.obs_lidar([adv_goal],0)
				concat_obs = np.hstack((action, lidar_to_adv)).astype('float32')
				# in variant 2 and 3, adversary is not further trained 
				delta = adversary.act(concat_obs)
				adv_action = action + delta
				env_obs, env_r, done, _ = env.step(adv_action)

				# env.render()
				# env.render_area(adv_goal , 0.3, COLOR_BUTTON, 'adv_goal', alpha=0.5)

				dist_adv_goal = env.dist_xy(adv_goal)
				# penalty for adversary entering nominal goal
				goal_penalty = 0
				if env.dist_xy(env.goal_pos) <= 0.3:
					goal_penalty = -1
					if env.dist_xy(adv_goal) <= 0.3:
						goal_penalty = 0

				adv_r = (last_dist_adv_goal - dist_adv_goal)*1 + goal_penalty

				#manual scaling of rewards since env rewards not going through wrapper
				adv_r = adv_r*1e-2
				adv_r = adv_r.astype('float32')
				last_dist_adv_goal = dist_adv_goal

				#re-sample new pair of goals if nominal goal reached
				if  env.dist_xy(env.goal_pos) <= 0.35:
					_ = env.reset()
					adv_goal = env.goal_pos
					env_obs = env.reset()
					last_dist_adv_goal = env.dist_xy(adv_goal)

				#re-sample new pair of goals if adversarial goal reached
				if  env.dist_xy(adv_goal) <= 0.3:
					adv_r += (1*1e-2)
					_ = env.reset()
					adv_goal = env.goal_pos
					env_obs = env.reset()
					last_dist_adv_goal = env.dist_xy(adv_goal)

				env_R += env_r
				adv_R += adv_r
				t += 1
				i += 1
			 
			adv_Rs.append(adv_R)
			env_Rs.append(env_R)

			if i % 1000 == 0:
				print('Step:', i, 'R:', env_R, 'adv_R:', adv_R)
				print('')
				print('statistics:', agent.get_statistics())
			agent.stop_episode_and_train(env_obs, env_r, done)

	stats = np.array((adv_Rs, env_Rs), dtype=float)
	print(stats)
	if os.path.exists(args.save_dir) == False:
		os.makedirs(args.save_dir)
		os.makedirs(args.save_dir + '/weights' )

	np.save(args.save_dir +'/' + args.env + '_'+ str(args.variant) + '_' + str(args.seed) + '.npy', stats)
	agent.save(args.save_dir +'/weights/' + args.env + '_'+ str(args.variant) + '_' + str(args.seed))


if __name__ == '__main__':
	main()