# Copyright 2019 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""
Internal module for the LoadDockerNodes Action.

LoadDockerNodes is an Action that controls the lifecycle of a sandboxed environment running nodes
as a Docker container. This Action is not exported and should only be used internally.
"""

from asyncio import CancelledError, Future, Task
from concurrent.futures import ThreadPoolExecutor
import shlex
from threading import Lock
from types import GeneratorType
from typing import List, Optional

import docker
from docker.errors import ImageNotFound

import launch
from launch import Action, LaunchContext
from launch.event import Event
from launch.event_handlers import OnShutdown
from launch.some_actions_type import SomeActionsType
from launch.utilities import create_future, perform_substitutions

from launch_ros_sandbox.descriptions.docker_policy import DockerPolicy
from launch_ros_sandbox.descriptions.sandboxed_node import SandboxedNode


def _containerized_cmd(entrypoint: str, package: str, executable: str) -> List[str]:
    """Prepare the command for executing within the Docker container."""
    # Use ros2 CLI command to find the executable
    return shlex.split(entrypoint) + ['ros2', 'run', package, executable]


class LoadDockerNodes(Action):
    """
    LoadDockerNodes is an Action that controls the sandbox environment spawned by `DockerPolicy`.

    LoadDockerNodes should only be constructed by `DockerPolicy.apply`.
    """

    def __init__(
        self,
        policy: DockerPolicy,
        node_descriptions: List[SandboxedNode],
        **kwargs
    ) -> None:
        """
        Construct the LoadDockerNodes Action.

        Parameters regarding initialization are copied here.
        Most of the arguments are forwarded to Action.
        """
        super().__init__(**kwargs)
        self._policy = policy
        self._node_descriptions = node_descriptions
        self._completed_future = None  # type: Optional[Future]
        self._started_task = None  # type: Optional[Task]
        self._container = None  # type: Optional[docker.models.containers.Container]
        self._shutdown_lock = Lock()
        self._docker_client = docker.from_env()
        self.__logger = launch.logging.get_logger(__name__)
        self._executor = ThreadPoolExecutor(max_workers=len(node_descriptions))

    def _pull_docker_image(self) -> None:
        """
        Pull the docker image.

        This will download the Docker image if it is not currently cached and will update it if its
        out of date.

        :raises ImageNotFound if Docker cannot find the remote repo for the image to pull
        """
        self.__logger.info('Pulling image {}'.format(self._policy.image_name))

        # This method may throw an ImageNotFound exception. Let the exception propogate upwards
        self._docker_client.images.pull(
            self._policy.repository,
            tag=self._policy.tag
        )

    def _start_docker_container(self) -> None:
        """
        Start Docker container.

        Run arguments will be forwarded to the containers run command if they exist.
        """
        tmp_run_args = self._policy.run_args or {}

        # This method may throw an ImageNotFound exception. Let the exception propogate upwards
        self._container = self._docker_client.containers.run(
            self._policy.image_name,
            detach=True,
            auto_remove=True,
            tty=True,
            name=self._policy.container_name,
            **tmp_run_args
        )

        self.__logger.info('Running Docker container: \"{}\"'.format(self._policy.container_name))

    def _load_nodes_in_docker(
        self,
        context: LaunchContext
    ) -> None:
        """Load all nodes into Docker container."""
        if self._container is None:
            self.__logger.error('Unable to load nodes into Docker container: '
                                'no active Docker container!')
            return

        for description in self._node_descriptions:
            package_name = perform_substitutions(
                context=context,
                subs=description.package
            )

            executable_name = perform_substitutions(
                context=context,
                subs=description.node_executable
            )

            cmd = _containerized_cmd(
                entrypoint=self._policy.entrypoint,
                package=package_name,
                executable=executable_name
            )

            log_generator = self._container.exec_run(
                cmd=cmd,
                tty=True,
                stream=True,
            )

            context.asyncio_loop.run_in_executor(self._executor, self._handle_logs, log_generator)

            self.__logger.debug('Running \"{}\" in container: \"{}\"'
                                .format(cmd, self._policy.container_name))

    def _handle_logs(
        self,
        log_generator: GeneratorType
    ) -> None:
        """
        Process the logs from a container and print to the logger.

        Expects the `log generator` returned from Docker-py's container.exec_run.
        The generator blocks until a new log chunk is available.
        The log chunk is of type `bytes`, so it must be decoded before its sent to the logger.
        """
        for log in log_generator:
            if not log:
                pass  # Sometimes we receive None
            elif isinstance(log, GeneratorType):
                for l in log:
                    self.__logger.info(l.decode('utf-8').strip())
            else:
                try:
                    self.__logger.info(log.decode('utf-8').strip())
                except (UnicodeDecodeError, AttributeError):
                    self.__logger.exception('Unable to print log of type {}'.format(type(log)))

    async def _start_docker_nodes(
        self,
        context: LaunchContext
    ) -> None:
        """
        Start the Docker container and load all nodes into it.

        This will first attempt to pull the docker image, start the docker container, and then load
        all of the nodes.

        """
        # Try to pull the image and warn if it cannot be found.
        try:
            self._pull_docker_image()
        except ImageNotFound as ex:
            self.__logger.warn('Image "{}" could not be pulled but may be found locally.'
                               .format(self._policy.image_name))
            self.__logger.debug(ex)

        # Try to run the image (even if it can't be pulled.) It might be available locally
        # Log an error if it cannot be found and cancel the future to signal that there is no work.
        try:
            self._start_docker_container()
        except ImageNotFound as ex:
            self.__logger.error(
                'Image "{}" could not be found; execution of container "{}" failed.'
                .format(self._policy.image_name, self._policy.container_name))
            self.__logger.debug(ex)

            with self._shutdown_lock:
                if self._completed_future is not None:
                    self._completed_future.cancel()
                    self._completed_future = None

            return

        self._load_nodes_in_docker(context)

    def get_asyncio_future(self) -> Optional[Future]:
        """Return the asyncio Future that represents the lifecycle of the Docker container."""
        return self._completed_future

    def execute(
        self,
        context: LaunchContext
    ) -> Optional[List[Action]]:
        """
        Execute the ROS 2 sandbox inside Docker.

        This will start the Docker container and run each ROS 2 node from inside that container.
        There is no additional work required, so this function always returns None.
        """
        context.register_event_handler(
            OnShutdown(
                on_shutdown=self.__on_shutdown
            )
        )

        self._completed_future = create_future(context.asyncio_loop)

        self._started_task = context.asyncio_loop.create_task(
            self._start_docker_nodes(context)
        )

        return None

    def __on_shutdown(
        self,
        event: Event,
        context: LaunchContext
    ) -> Optional[SomeActionsType]:
        """
        Run when the shutdown signal has been received.

        This will cancel the started task, if running, call cancel
        on the completed future, and stop the container.

        """
        with self._shutdown_lock:

            # if still starting cancel
            if self._started_task is not None:
                try:
                    self._started_task.cancel()
                except CancelledError:
                    self._started_task = None

            if self._completed_future is not None:
                self._executor.shutdown(wait=False)
                self._completed_future.cancel()
                self._completed_future = None

                if self._container is not None:
                    self._container.stop()
                    self._container = None

        return None
