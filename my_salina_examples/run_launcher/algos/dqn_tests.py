import sys
import os
import time
import numpy as np

import gym
import my_gym

from gym.wrappers import TimeLimit
from omegaconf import DictConfig, OmegaConf
from salina import instantiate_class, get_arguments, get_class
from salina.workspace import Workspace
from salina.agents import Agents, TemporalAgent

from salina.rl.functionalb import gae

from salina.logger import TFLogger
import hydra

import copy
import time

import torch
import torch.nn as nn
import torch.autograd as autograd
from torch.autograd import detect_anomaly

from my_salina_examples.models.critics import DiscreteQAgent
from my_salina_examples.models.envs import AutoResetEnvAgent, NoAutoResetEnvAgent
from my_salina_examples.models.loggers import Logger
from my_salina_examples.chrono import Chrono

# HYDRA_FULL_ERROR = 1


# Create the DQN Agent
def create_dqn_agent(cfg, train_env_agent, eval_env_agent):
    obs_size, act_size = train_env_agent.get_obs_and_actions_sizes()

    q_agent = TemporalAgent(DiscreteQAgent(obs_size, act_size, cfg.algorithm.architecture.hidden_size))
    tr_agent = Agents(train_env_agent, q_agent)
    ev_agent = Agents(eval_env_agent, q_agent)

    # Get an agent that is executed on a complete workspace
    train_agent = TemporalAgent(tr_agent)
    eval_agent = TemporalAgent(ev_agent)
    train_agent.seed(cfg.algorithm.seed)
    return train_agent, eval_agent, q_agent


def make_gym_env(env_name):
    return gym.make(env_name)


# Configure the optimizer
def setup_optimizers(cfg, q_agent):
    optimizer_args = get_arguments(cfg.optimizer)
    parameters = q_agent.parameters()
    optimizer = get_class(cfg.optimizer)(parameters, **optimizer_args)
    return optimizer

# Take the action
def compute_critic_loss(cfg, reward, must_bootstrap, q_value, action):
    # Compute temporal difference
    target = reward[:-1] + cfg.algorithm.discount_factor * q_value.max(0)[1:].detach() * must_bootstrap.float()
    td = target - q_value[action][:-1]
    # Compute critic loss
    td_error = td ** 2
    critic_loss = td_error.mean()
    return critic_loss, td


def run_dqn(cfg, max_grad_norm=0.5):
    # 1)  Build the  logger
    chrono = Chrono()
    logger = Logger(cfg)
    best_reward = -10e9

    # 2) Create the environment agent
    train_env_agent = AutoResetEnvAgent(cfg, n_envs=cfg.algorithm.n_envs)
    eval_env_agent = NoAutoResetEnvAgent(cfg, n_envs=cfg.algorithm.nb_evals)

    # 3) Create the A2C Agent
    a2c_agent, eval_agent, q_agent = create_dqn_agent(cfg, train_env_agent, eval_env_agent)

    # 5) Configure the workspace to the right dimension
    # Note that no parameter is needed to create the workspace.
    # In the training loop, calling the agent() and critic_agent()
    # will take the workspace as parameter
    train_workspace = Workspace()  # Used for training

    # 6) Configure the optimizer over the a2c agent
    optimizer = setup_optimizers(cfg, q_agent)
    nb_steps = 0
    tmp_steps = 0

    # 7) Training loop
    for epoch in range(cfg.algorithm.max_epochs):
        # Execute the agent in the workspace
        if epoch > 0:
            train_workspace.zero_grad()
            train_workspace.copy_n_last_steps(1)
            q_agent(train_workspace, t=1, n_steps=cfg.algorithm.n_steps - 1, stochastic=True)
        else:
            q_agent(train_workspace, t=0, n_steps=cfg.algorithm.n_steps, stochastic=True)

        # Ajouter de l'exploration
  
        nb_steps += cfg.algorithm.n_steps * cfg.algorithm.n_envs

        transition_workspace = train_workspace.get_transitions()

        q_value, done, truncated, reward, action = transition_workspace[
            "q_value", "env/done", "env/truncated", "env/reward", "action"]

        # Determines whether values of the critic should be propagated
        # True if the episode reached a time limit or if the task was not done
        # See https://colab.research.google.com/drive/1W9Y-3fa6LsPeR6cBC1vgwBjKfgMwZvP5?usp=sharing
        must_bootstrap = torch.logical_or(~done[1], truncated[1])

        # Compute critic loss
        critic_loss, td = compute_critic_loss(cfg, reward, must_bootstrap, q_value, action)

        # Store the loss for tensorboard display
        logger.add_loss(nb_steps, critic_loss)

        optimizer.zero_grad()
        critic_loss.backward()
        torch.nn.utils.clip_grad_norm_(a2c_agent.parameters(), max_grad_norm)
        optimizer.step()

        if nb_steps - tmp_steps > cfg.algorithm.eval_interval:
            tmp_steps = nb_steps
            eval_workspace = Workspace()  # Used for evaluation
            eval_agent(eval_workspace, t=0, stop_variable="env/done", stochastic=False)
            rewards = eval_workspace["env/cumulated_reward"][-1]
            mean = rewards.mean()
            logger.add_log("reward", mean, nb_steps)
            print(f"epoch: {epoch}, reward: {mean}")
            if cfg.save_best and mean > best_reward:
                best_reward = mean
                directory = "./dqn_critic/"
                if not os.path.exists(directory):
                    os.makedirs(directory)
                filename = directory + "a2c_" + str(mean.item()) + ".agt"
                eval_agent.save_model(filename)
                # critic = q_agent.agent
    chrono.stop()


# @hydra.main(config_path="./configs/", config_name="a2c_pendulum.yaml")
# @hydra.main(config_path="./configs/", config_name="a2c_cartpolecontinuous.yaml")
@hydra.main(config_path="./configs/", config_name="a2c_cartpole.yaml")
def main(cfg: DictConfig):
    # print(OmegaConf.to_yaml(cfg))
    torch.manual_seed(cfg.algorithm.seed)
    run_dqn(cfg)


if __name__ == "__main__":
    sys.path.append(os.getcwd())
    main()