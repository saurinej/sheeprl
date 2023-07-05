import os
import time
from dataclasses import asdict
from datetime import datetime

import gymnasium as gym
import torch
from lightning import Fabric
from lightning.fabric.fabric import _is_using_cli
from lightning.fabric.loggers import TensorBoardLogger
from lightning.fabric.plugins.collectives import TorchCollective
from torch.optim import Adam
from torchmetrics import MeanMetric
from tqdm import tqdm

from sheeprl.algos.muzero.agent import RecurrentMuzero
from sheeprl.algos.muzero.args import MuzeroArgs
from sheeprl.algos.muzero.loss import policy_loss, reward_loss, value_loss
from sheeprl.algos.muzero.utils import Node, make_env, test, visit_softmax_temperature
from sheeprl.data.buffers import Trajectory, TrajectoryReplayBuffer
from sheeprl.utils.callback import CheckpointCallback
from sheeprl.utils.metric import MetricAggregator
from sheeprl.utils.parser import HfArgumentParser
from sheeprl.utils.registry import register_algorithm


@register_algorithm()
def main():
    parser = HfArgumentParser(MuzeroArgs)
    args: MuzeroArgs = parser.parse_args_into_dataclasses()[0]

    # Initialize Fabric
    fabric = Fabric(callbacks=[CheckpointCallback()])
    if not _is_using_cli():
        fabric.launch()
    rank = fabric.global_rank
    device = fabric.device
    fabric.seed_everything(args.seed)
    torch.backends.cudnn.deterministic = args.torch_deterministic

    # Set logger only on rank-0 but share the logger directory: since we don't know
    # what is happening during the `fabric.save()` method, at least we assure that all
    # ranks save under the same named folder.
    # As a plus, rank-0 sets the time uniquely for everyone
    world_collective = TorchCollective()
    if fabric.world_size > 1:
        world_collective.setup()
        world_collective.create_group()
    if rank == 0:
        root_dir = (
            args.root_dir
            if args.root_dir is not None
            else os.path.join("logs", "muzero", datetime.today().strftime("%Y-%m-%d_%H-%M-%S"))
        )
        run_name = (
            args.run_name
            if args.run_name is not None
            else f"{args.env_id}_{args.exp_name}_{args.seed}_{int(time.time())}"
        )
        logger = TensorBoardLogger(root_dir=root_dir, name=run_name)
        fabric._loggers = [logger]
        log_dir = logger.log_dir
        fabric.logger.log_hyperparams(asdict(args))
        if fabric.world_size > 1:
            world_collective.broadcast_object_list([log_dir], src=0)
    else:
        data = [""]
        world_collective.broadcast_object_list(data, src=0)
        log_dir = data[0]
        os.makedirs(log_dir, exist_ok=True)

    # Environment setup
    env = make_env(
        args.env_id,
        args.seed + rank,
        rank,
        args.capture_video,
        logger.log_dir,
        "train",
        frame_stack=1,
        action_repeat=args.action_repeat,
        vector_env_idx=rank,
    )()
    assert isinstance(env.action_space, gym.spaces.Discrete), "Only discrete action space is supported"

    # Create the model
    agent = RecurrentMuzero(num_actions=env.action_space.n).to(device)
    optimizer = Adam(agent.parameters(), lr=args.lr, eps=1e-4, weight_decay=args.weight_decay)
    optimizer = fabric.setup_optimizers(optimizer)

    # Metrics
    with device:
        aggregator = MetricAggregator(
            {
                "Rewards/rew_avg": MeanMetric(),
                "Game/ep_len_avg": MeanMetric(),
                "Time/step_per_second": MeanMetric(),
                "Loss/value_loss": MeanMetric(),
                "Loss/policy_loss": MeanMetric(),
                "Loss/reward_loss": MeanMetric(),
                "Loss/total_loss": MeanMetric(),
                "Gradient/gradient_norm": MeanMetric(),
                "Info/policy_entropy": MeanMetric(),
            }
        )

    # Local data
    buffer_size = args.buffer_capacity // int(fabric.world_size) if not args.dry_run else 1
    rb = TrajectoryReplayBuffer(max_num_trajectories=buffer_size, device=device, memmap=args.memmap_buffer)

    # Global variables
    start_time = time.perf_counter()
    num_updates = int(args.total_steps // args.num_players) if not args.dry_run else 1
    args.learning_starts = args.learning_starts // args.num_players if not args.dry_run else 0

    for update_step in range(1, num_updates + 1):
        with torch.no_grad():
            # reset the episode at every update
            with device:
                # Get the first environment observation and start the optimization
                obs: torch.Tensor = torch.tensor(env.reset(seed=args.seed)[0], device=device).view(
                    1, 3, 64, 64
                )  # shape (1, C, H, W)
                rew_sum = 0.0

            steps_data = None
            print(f"Update {update_step} started")
            for trajectory_step in tqdm(range(0, args.max_trajectory_len)):
                node = Node(prior=0, image=obs, device=device)

                # start MCTS
                node.mcts(agent, args.num_simulations, args.gamma, args.dirichlet_alpha, args.exploration_fraction)

                # Select action based on the visit count distribution and the temperature
                visits_count = torch.tensor([child.visit_count for child in node.children.values()])
                temperature = visit_softmax_temperature(training_steps=agent.training_steps)
                visits_count = visits_count / (temperature * args.num_simulations)
                action = torch.distributions.Categorical(logits=visits_count).sample()
                # Single environment step
                next_obs, reward, done, truncated, info = env.step(action.cpu().numpy().reshape(env.action_space.shape))
                rew_sum += reward

                # Store the current step data
                trajectory_step_data = Trajectory(
                    {
                        "policies": visits_count.reshape(1, 1, -1),
                        "actions": action.reshape(1, 1, -1),
                        "observations": obs.unsqueeze(0),
                        "rewards": torch.tensor([reward]).reshape(1, 1, -1),
                        "values": node.value().reshape(1, 1, -1),
                    },
                    batch_size=(1, 1),
                    device=device,
                )
                if steps_data is None:
                    steps_data = trajectory_step_data
                else:
                    steps_data = torch.cat([steps_data, trajectory_step_data])

                if done or truncated:
                    break
                else:
                    with device:
                        obs = torch.tensor(next_obs).view(1, 3, 64, 64)

            fabric.print(f"Rank-{rank}: update_step={update_step}, reward={rew_sum}")
            aggregator.update("Rewards/rew_avg", rew_sum)
            aggregator.update("Game/ep_len_avg", trajectory_step)
            print("Finished episode")
            rb.add(trajectory=steps_data)

        if update_step >= args.learning_starts - 1:
            for _ in range(args.update_epochs):
                # We sample one time to reduce the communications between processes
                data = rb.sample(args.chunks_per_batch, args.chunk_sequence_len)

                target_rewards = data["rewards"].squeeze(-1)
                target_values = data["values"].squeeze(-1)
                target_policies = data["policies"].squeeze()
                observations: torch.Tensor = data["observations"].squeeze(2)  # shape should be (L, N, C, H, W)
                actions = data["actions"].squeeze(2)

                hidden_states, policy_0, value_0 = agent.initial_inference(
                    observations[0]
                )  # in shape should be (N, C, H, W)
                # Policy loss
                pg_loss = policy_loss(policy_0, target_policies[0])
                # Value loss
                v_loss = value_loss(value_0, target_values[0])
                # Reward loss
                r_loss = torch.tensor(0.0, device=device)
                entropy = torch.distributions.Categorical(logits=policy_0.detach()).entropy().unsqueeze(0)

                for sequence_idx in range(1, args.chunk_sequence_len):
                    hidden_states, rewards, policies, values = agent.recurrent_inference(
                        actions[sequence_idx].unsqueeze(0).to(dtype=torch.float32), hidden_states
                    )  # action should be (1, N, 1)
                    # Policy loss
                    pg_loss += policy_loss(policies, target_policies[sequence_idx : sequence_idx + 1])
                    # Value loss
                    v_loss += value_loss(values, target_values[sequence_idx : sequence_idx + 1])
                    # Reward loss
                    r_loss += reward_loss(rewards, target_rewards[sequence_idx : sequence_idx + 1])
                    entropy += torch.distributions.Categorical(logits=policies.detach()).entropy()

                # Equation (1) in the paper, the regularization loss is handled by `weight_decay` in the optimizer
                loss = (pg_loss + v_loss + r_loss) / args.chunk_sequence_len

                optimizer.zero_grad(set_to_none=True)
                fabric.backward(loss)
                print("UPDATING")
                optimizer.step()

                # Update metrics
                aggregator.update("Loss/policy_loss", pg_loss.detach())
                aggregator.update("Loss/value_loss", v_loss.detach())
                aggregator.update("Loss/reward_loss", r_loss.detach())
                aggregator.update("Loss/total_loss", loss.detach())
                aggregator.update("Gradient/gradient_norm", agent.gradient_norm())
                aggregator.update("Info/policy_entropy", entropy.mean() / args.chunk_sequence_len)

        aggregator.update("Time/step_per_second", int(update_step / (time.perf_counter() - start_time)))
        fabric.log_dict(aggregator.compute(), update_step)
        aggregator.reset()

        if (
            (args.checkpoint_every > 0 and update_step % args.checkpoint_every == 0)
            or args.dry_run
            or update_step == num_updates
        ):
            state = {
                "agent": agent.state_dict(),
                "optimizer": optimizer.state_dict(),
                "args": asdict(args),
                "update_step": update_step,
            }
            ckpt_path = os.path.join(log_dir, f"checkpoint/ckpt_{update_step}_{fabric.global_rank}.ckpt")
            fabric.call("on_checkpoint_coupled", fabric=fabric, ckpt_path=ckpt_path, state=state)

    env.close()

    if fabric.is_global_zero:
        test_env = make_env(
            args.env_id,
            None,
            0,
            args.capture_video,
            fabric.logger.log_dir,
            "test",
            vector_env_idx=0,
        )()
        test(agent, test_env, fabric, args)


if __name__ == "__main__":
    main()
