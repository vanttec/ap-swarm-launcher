from contextlib import asynccontextmanager
from importlib.resources import path as resource_path
from itertools import count
from pathlib import Path
from trio import open_file, open_nursery, Path as AsyncPath
from typing import (
    AsyncIterator,
    ContextManager,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from .async_process_runner import (
    AsyncProcessRunner,
    AsyncProcessRunnerContext,
    ManagedAsyncProcess,
)
from .formations import Point
from .locations import (
    DEFAULT_LOCATION,
    FlatEarthToGPSCoordinateTransformation,
    GPSCoordinate,
)
from .utils import copy_file_async, maybe_temporary_working_directory

__all__ = (
    "SimulatedDroneSwarm",
    "SimulatedDroneSwarmContext",
)


def create_args_for_simulator(
    model: str = "quad",
    param_file: Optional[Union[str, Path]] = None,
    use_console: bool = False,
    home: GPSCoordinate = DEFAULT_LOCATION.origin,
    heading: float = 0,
    index: Optional[int] = 0,
    uarts: Optional[Dict[str, str]] = None,
    rc_input_port: Optional[int] = None,
    speedup: float = 1,
) -> List[str]:
    """Creates the argument list to execute the SITL simulator.

    Parameters:
        model: dynamics model of the simulated vehicle
        param_file: name or path of the parameter file that holds the default
            parameters of the vehicle
        use_console: whether the SITL simulator should use the console instead
            of a TCP port for its primary input / output stream
        home: the home position of the drone
        heading: the initial heading of the drone
        index: specifies the index of the drone when multiple simulators are
            being run on the same computer; each increase in the index shifts
            all the ports used by the drone by +10
        uarts: dictionary mapping SITL UART identifiers (from A to H) to roles.
            See the SITL documentation for the possible roles. Examples are:
            `tcp:localhost:5760` creates a TCP server socket on port 5760;
            `udpclient:localhost:14550` creates a UDP socket that sends packets
            to port 14550.
        rc_input_port: port to listen on for RC input
        speedup: simulation speedup factor; use 1 for real-time
    """
    result = ["-M", model, "--disable-fgview"]

    if index is not None:
        if index < 0:
            raise ValueError("index must be non-negative")

        result.extend(["-I", str(index)])

    if use_console:
        result.append("-C")

    if param_file:
        result.extend(["--defaults", str(param_file)])

    if rc_input_port is not None:
        result.extend(["--rc-in-port", str(rc_input_port)])

    if uarts:
        for uart_id, value in uarts.items():
            uart_id = uart_id.upper()
            result.append(f"--uart{uart_id}={value}")

    home_as_str = f"{home.lat:.7f},{home.lon:.7f},{home.amsl:.1f},{int(heading)}"
    result.extend(["--home", home_as_str])

    # Add --speedup argument unconditionally because there is a bug in the SITL
    # as of ArduCopter 4.2.1 that crashes the SITL on macOS if the speedup is
    # not set explicitly
    result.extend(["--speedup", str(speedup)])

    return result


async def start_simulator(
    executable: Path,
    *,
    runner: AsyncProcessRunnerContext,
    name: str = "ArduCopter",
    cwd: Optional[Union[str, Path]] = None,
    **kwds,
) -> ManagedAsyncProcess:
    """Starts the SITL simulator, supervised by the given asynchronous process
    runner.

    Keyword arguments not mentioned here are forwarded to
    `create_args_for_simulator()`.

    Keyword arguments:
        param_file: name or path of the parameter file that holds the default
            parameters of the
        runner: the process runner that will supervise the execution of the
            simulator
        name: the name of the spawned process in the process runner
        cwd: the current working directory of the ArduCopter process; this is
            the folder where the EEPROM contents will be read from / written to

    Returns:
        the ManagedProcess instance that represents the launched simulator
    """
    args = [str(executable)]
    args.extend(create_args_for_simulator(**kwds))

    if cwd is not None:
        cwd = str(cwd)

    stream_stdout = "-C" not in args
    use_stdin = "-C" in args

    process = await runner.start(
        args,
        name=name,
        daemon=True,
        cwd=cwd,
        stream_stdout=stream_stdout,
        use_stdin=use_stdin,
    )

    return process


class SimulatedDroneSwarm:
    """Object that is responsible for managing a simulated drone swarm by
    spawing the appropriate ArduPilot SITL processes for each simulated
    drone.
    """

    _executable: Path
    _swarm_dir: Optional[Path] = None
    _tcp_base_port: Optional[int] = None

    def __init__(
        self,
        executable: Path,
        dir: Optional[Path] = None,
        params: Optional[Sequence[Union[str, Path, Tuple[str, float]]]] = None,
        coordinate_system: Optional[FlatEarthToGPSCoordinateTransformation] = None,
        amsl: Optional[float] = None,
        default_heading: Optional[float] = None,
        gcs_address: str = "127.0.0.1:14550",
        multicast_address: Optional[str] = None,
        tcp_base_port: Optional[int] = None,
    ):
        """Constructor.

        Parameters:
            executable: full path to the Ardupilot SITL executable
            dir: the configuration directory of the swarm. When omitted, a
                temporary directory will be created for the content related
                to the swarm.
            coordinate_system: transformation that converts local coordinates
                to GPS coordinates. Defaults to a coordinate system derived
                from `DEFAULT_LOCATION`
            default_heading: the default heading of the drones in the swarm;
                `None` means to align it with the X axis of the local
                coordinate system (which is also the default)
            params: parameters to pass to the simulator; it must be a list where
                each entry is either a Path object pointing to a parameter file
                or a name-value pair as a tuple
            gcs_address: target IP address and port where the simulated drones
                will send their status packets
            multicast_address: optional multicast IP address and port where the
                simulated drones will listen for packets that are intended to
                reach all the drones in the swarm
            tcp_base_port: TCP port number where the simulated drones will
                be available via a TCP connection. This is the base port
                number; each drone will get a new TCP port, counting upwards
                from this base port number.
        """
        self._executable = Path(executable)
        self._dir = Path(dir) if dir else None
        self._params = list(params) if params else []
        self._tcp_base_port = int(tcp_base_port) if tcp_base_port else None

        if coordinate_system:
            self._coordinate_system = coordinate_system
        else:
            self._coordinate_system = DEFAULT_LOCATION.coordinate_system

        self._default_heading = (
            float(default_heading) % 360
            if default_heading is not None
            else self._coordinate_system.orientation
        )

        self._gcs_address = gcs_address
        self._multicast_address = multicast_address

        self._index_generator = count(1)

        self._nursery = None
        self._runner = None
        self._swarm_dir = None

    @asynccontextmanager
    async def use(self) -> AsyncIterator["SimulatedDroneSwarmContext"]:
        """Async context manager that starts the swarm when entered and stops
        the swarm when exited.
        """
        async with open_nursery() as self._nursery:
            with maybe_temporary_working_directory(self._dir) as self._swarm_dir:
                async with AsyncProcessRunner(sidebar_width=5) as self._runner:
                    try:
                        yield SimulatedDroneSwarmContext(self)
                    finally:
                        self._nursery = None
                        self._runner = None
                        self._swarm_dir = None

    def _request_stop(self):
        """Requests the simulator processes of the swarm to stop."""
        if self._runner:
            self._runner.request_stop()
        if self._nursery:
            self._nursery.cancel_scope.cancel()

    async def _start_simulated_drone(
        self,
        home: Point,
        heading: Optional[float] = None,
    ) -> ManagedAsyncProcess:
        """Starts the simulator process for a single drone with the given
        parameters.

        Parameters:
            home: the home position of the drone, in flat Earth coordinates
            heading: the initial heading of the drone; `None` means to use the
                swarm-specific default
        """
        assert self._runner is not None
        assert self._swarm_dir is not None

        geodetic_home = self._coordinate_system.to_gps((home[0], home[1], 0))
        heading = float(heading) if heading is not None else self._default_heading

        index = next(self._index_generator)

        drone_id = "{0:03}".format(index)
        drone_dir = self._swarm_dir / "drones" / drone_id
        drone_fs_dir = drone_dir / "fs"

        own_param_file = drone_dir / "default.param"

        tcp_port = self._tcp_base_port + index - 1 if self._tcp_base_port else None

        await AsyncPath(drone_dir).mkdir(parents=True, exist_ok=True)  # type: ignore

        async with await open_file(own_param_file, "wb+") as fp:
            for param_source in self._params:
                if isinstance(param_source, str):
                    if param_source.startswith("embedded://"):
                        param_source = use_embedded_param_file(
                            param_source[len("embedded://") :].lstrip("/")
                        )
                    else:
                        param_source = Path(param_source)

                if isinstance(param_source, tuple):
                    name, value = param_source
                    await fp.write(f"{name}\t{value}\n".encode("utf-8"))
                elif hasattr(param_source, "__enter__"):
                    with param_source as path:
                        await copy_file_async(path, fp)
                        await fp.write(b"\n")
                elif param_source is not None:
                    await copy_file_async(param_source, fp)
                    await fp.write(b"\n")

            await fp.write(f"SYSID_THISMAV\t{index}\n".encode("utf-8"))

            if self._multicast_address:
                # We need a second serial port for receiving multicast traffic
                # (which is used to simulate broadcast). At the same time, we
                # disable MAVLink forwarding to/from this port
                await fp.write("SERIAL1_PROTOCOL\t2\n".encode("utf-8"))
                await fp.write("SERIAL1_OPTIONS\t1024\n".encode("utf-8"))

            if tcp_port:
                # We also need a serial port for receiving direct traffic from
                # the TCP port associated to the UAV.
                await fp.write("SERIAL2_PROTOCOL\t2\n".encode("utf-8"))

        await AsyncPath(drone_fs_dir).mkdir(parents=True, exist_ok=True)  # type: ignore

        process = await start_simulator(
            self._executable,
            runner=self._runner,
            name=str(index),
            param_file=own_param_file,
            home=geodetic_home,
            heading=heading,
            index=index - 1,
            cwd=drone_fs_dir,
            uarts={
                # localhost is not okay, at least not on macOS
                "A": f"udpclient:{self._gcs_address}",
                # the following two ports might not be used -- they must be there
                # to prevent firewall warnings on macOS
                "C": (
                    f"mcast:{self._multicast_address}"
                    if self._multicast_address
                    else "udpclient:127.0.0.1:14555"
                ),
                "D": (f"tcp:{tcp_port}" if tcp_port else "udpclient:127.0.0.1:14552"),
            },
        )

        return process


def use_embedded_param_file(name: str) -> ContextManager[Path]:
    """Context manager that ensures that the embedded ArduPilot SITL parameter
    file with the given name is accessible on the filesystem, extracting it to
    a temporary file if needed. The temporary file (if any) is cleaned up when
    exiting the context.
    """
    return resource_path(f"{__package__}.resources", name)


class SimulatedDroneSwarmContext:
    """Context object returned from `SimulatedDroneSwarm.use()` that the user
    can use to add new drones to the swarm.
    """

    def __init__(self, swarm: SimulatedDroneSwarm):
        self._swarm = swarm

    async def add_drone(
        self,
        home: Point,
        heading: Optional[float] = None,
    ) -> ManagedAsyncProcess:
        """Adds a new drone to the swarm and starts the corresponding simulator
        process.

        Parameters:
            home: the home position of the drone, in flat Earth coordinates
            heading: the initial heading of the drone; `None` means to use the
                swarm-specific default
        """
        return await self._swarm._start_simulated_drone(home=home, heading=heading)

    def request_stop(self):
        """Requests the drone swarm to stop the simulation and shut down
        gracefully.
        """
        self._swarm._request_stop()
