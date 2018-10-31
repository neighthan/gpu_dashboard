from time import time, sleep
from typing import Dict, Any, Sequence
from collections import namedtuple
from threading import Thread, Lock
from ssh import SSHConnection
from gpu_utils import get_gpus, GPU


_Process = namedtuple(
    "NewProcess", ["command", "gpu_num", "mem_needed", "util_needed", "timestamp"]
)
_smi_command = (
    "nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv"
)


class Machine:
    def __init__(
        self,
        _id: str,
        address: str,
        username: str,
        ssh_password: str,
        jobs_db,
        skip_gpus: Sequence[int] = (),
        gpu_runner_on: bool = False,
    ):
        self._id = _id
        self.address = address
        self.username = username
        self.jobs_db = jobs_db
        self.skip_gpus = skip_gpus
        self.gpu_runner_on = gpu_runner_on
        self.new_processes = []
        self._client = SSHConnection(
            self.address, self.username, ssh_password, auto_add_host=True
        )
        self._client_lock = Lock()

    def dashboard_data(self) -> Dict[str, Any]:
        return {
            "_id": self._id,
            "address": self.address,
            "username": self.username,
            "gpu_runner_on": self.gpu_runner_on,
        }

    def execute(self, command: str, codec: str = "utf-8") -> str:
        """
        Runs `command` using the SSHConnection for this Machine and returns stdout
        :param command: *single-line* command to run
        :param codec: codec to use to decode the standard output from running `command`
        :returns: decoded stdout
        """

        with self._client_lock:
            return self._client.execute(command, codec)

    def start(self, sleep_time: int = 30):
        def handle_machine(machine: Machine, sleep_time: int):
            while True:
                if machine.gpu_runner_on:
                    machine.start_jobs()
                sleep(sleep_time)

        thread = Thread(target=lambda: handle_machine(self, sleep_time), daemon=True)
        thread.start()

    def start_jobs(self, n_passes: int = 2, keep_time: int = 60) -> None:
        """
        :param n_passes: number of times to query the state of the GPUs; the utilization
          currently used on each GPU is assumed to be the mean across these passes, and
          the memory used is the max
        :param keep_time: how long to keep a started process in the new_processes list
          before removing it (a process is removed immediately if we see it running).
          Leaving a process in the new_processes list means that we assume that the
          resources it has requested are already "reserved" even though we don't see
          them being used yet on the GPU. Removing a processes from this list lets
          other processes be started with those resources. (`keep_time` is in seconds)
        """
        while True:  # place jobs for this machine until you can't place any more
            job = self.jobs_db.find_one({"machine": self._id}, sort=[("util", 1)])
            if not job:  # no more queued jobs for this machine
                break

            # check if there's a gpu you can run this job on (enough memory and util free)
            gpus = {}
            processes = self.new_processes

            for _ in range(n_passes):
                for gpu in get_gpus(
                    self.skip_gpus, info_string=self.execute(_smi_command)
                ):
                    try:
                        gpus[gpu.num].append(gpu)
                    except KeyError:
                        gpus[gpu.num] = [gpu]

            # remove processes that have shown up on the GPU
            # if a process doesn't show up on the GPU after enough time, assume it had an error and crashed; remove
            info_string = self.execute("nvidia-smi")
            now = time()
            processes = [
                process
                for process in processes
                if process.command not in info_string
                and now - process.timestamp < keep_time
            ]

            # subtract mem and util used by new processes from that which is shown to be free
            mem_newly_used = {gpu_num: 0 for gpu_num in gpus}
            util_newly_used = {gpu_num: 0 for gpu_num in gpus}
            for process in processes:
                mem_newly_used[process.gpu_num] += process.mem_needed
                util_newly_used[process.gpu_num] += process.util_needed

            # set mem_free to max from each pass, util_free to mean
            gpus = [
                GPU(
                    num=num,
                    mem_free=max([gpu.mem_free for gpu in gpu_list])
                    - mem_newly_used[num],
                    util_free=sum([gpu.util_free for gpu in gpu_list]) / len(gpu_list)
                    - util_newly_used[num],
                    mem_used=None,  # don't need mem/util used now
                    util_used=None,
                )
                for (num, gpu_list) in gpus.items()
            ]

            gpus = [
                gpu
                for gpu in gpus
                if gpu.mem_free >= job["mem"] and gpu.util_free >= job["util"]
            ]

            try:
                best_gpu = max(gpus, key=lambda gpu: gpu.util_free)
            except ValueError:  # max gets no gpus because none have enough mem_free and util_free
                break  # can't place anything on this machine

            job_cmd = job["cmd"].format(best_gpu.num)
            # app.logger.info(f"Starting job: {job_cmd} ({self._id})")
            self.execute(
                f"({job_cmd} >> ~/.gpu_log 2>&1 &)"
            )  # make sure to background the script

            processes.append(
                _Process(
                    job_cmd,
                    best_gpu.num,
                    mem_needed=job["mem"],
                    util_needed=job["util"],
                    timestamp=time(),
                )
            )

            # this job is running, so remove it from the list
            self.jobs_db.remove({"_id": job["_id"]})