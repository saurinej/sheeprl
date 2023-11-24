from __future__ import annotations

from typing import Any, Dict

import gymnasium as gym
from lightning import Fabric

from sheeprl.algos.dreamer_v3.agent import PlayerDV3
from sheeprl.algos.dreamer_v3.utils import test
from sheeprl.algos.p2e_dv3.agent import build_models
from sheeprl.utils.env import make_env
from sheeprl.utils.logger import create_tensorboard_logger, get_log_dir
from sheeprl.utils.registry import register_evaluation


@register_evaluation(algorithms=["p2e_dv3_exploration", "p2e_dv3_finetuning"])
def evaluate(fabric: Fabric, cfg: Dict[str, Any], state: Dict[str, Any]):
    logger = create_tensorboard_logger(fabric, cfg)
    if logger and fabric.is_global_zero:
        fabric._loggers = [logger]
        fabric.logger.log_hyperparams(cfg)
    log_dir = get_log_dir(fabric, cfg.root_dir, cfg.run_name)

    env = make_env(
        cfg,
        cfg.seed,
        0,
        log_dir,
        "test",
        vector_env_idx=0,
    )()
    observation_space = env.observation_space
    action_space = env.action_space

    if not isinstance(observation_space, gym.spaces.Dict):
        raise RuntimeError(f"Unexpected observation type, should be of type Dict, got: {observation_space}")

    fabric.print("Encoder CNN keys:", cfg.algo.cnn_keys.encoder)
    fabric.print("Encoder MLP keys:", cfg.algo.mlp_keys.encoder)

    is_continuous = isinstance(action_space, gym.spaces.Box)
    is_multidiscrete = isinstance(action_space, gym.spaces.MultiDiscrete)
    actions_dim = (
        action_space.shape if is_continuous else (action_space.nvec.tolist() if is_multidiscrete else [action_space.n])
    )
    # Create the actor and critic models
    world_model, actor, _, _, _, _ = build_models(
        fabric,
        actions_dim,
        is_continuous,
        cfg,
        observation_space,
        state["world_model"],
        state["actor_task"],
    )
    player = PlayerDV3(
        world_model.encoder.module,
        world_model.rssm,
        actor.module,
        actions_dim,
        cfg.algo.player.expl_amount,
        cfg.env.num_envs,
        cfg.algo.world_model.stochastic_size,
        cfg.algo.world_model.recurrent_model.recurrent_state_size,
        fabric.device,
        discrete_size=cfg.algo.world_model.discrete_size,
        actor_type="task",
    )

    test(player, fabric, cfg, log_dir, sample_actions=True)
