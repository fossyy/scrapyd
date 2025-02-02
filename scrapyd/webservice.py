from __future__ import annotations

import base64
import functools
import gzip
import io
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import sys
import traceback
import uuid
import zipfile
from collections import defaultdict, deque
from datetime import datetime
from io import BytesIO
from subprocess import PIPE, Popen
from typing import ClassVar

import psutil
from twisted.logger import Logger
from twisted.web import error, http, resource

from scrapyd.exceptions import EggNotFoundError, ProjectNotFoundError, RunnerError

log = Logger()


def param(
    decoded: str,
    *,
    dest: str | None = None,
    required: bool = True,
    default=None,
    multiple: bool = False,
    type=str,  # noqa: A002 like Click
):
    encoded = decoded.encode()
    if dest is None:
        dest = decoded

    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, txrequest, *args, **kwargs):
            default_value = default() if callable(default) else default

            if encoded not in txrequest.args:
                if required:
                    raise error.Error(code=http.OK, message=b"'%b' parameter is required" % encoded)

                value = default_value
            else:
                values = (value.decode() if type is str else type(value) for value in txrequest.args.pop(encoded))
                try:
                    value = list(values) if multiple else next(values)
                except (UnicodeDecodeError, ValueError) as e:
                    raise error.Error(code=http.OK, message=b"%b is invalid: %b" % (encoded, str(e).encode())) from e

            kwargs[dest] = value

            return func(self, txrequest, *args, **kwargs)

        return wrapper

    return decorator


class SpiderList:
    cache: ClassVar = defaultdict(dict)

    def get(self, project, version, *, runner):
        """Return the ``scrapy list`` output for the project and version, using a cache if possible."""
        try:
            return self.cache[project][version]
        except KeyError:
            return self.set(project, version, runner=runner)

    def set(self, project, version, *, runner):
        """Calculate, cache and return the ``scrapy list`` output for the project and version, bypassing the cache."""

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "UTF-8"
        env["SCRAPY_PROJECT"] = project
        # If the version is not provided, then the runner uses the default version, determined by egg storage.
        if version:
            env["SCRAPYD_EGG_VERSION"] = version

        args = [sys.executable, "-m", runner, "list", "-s", "LOG_STDOUT=0"]
        process = Popen(args, stdout=PIPE, stderr=PIPE, env=env)
        stdout, stderr = process.communicate()
        if process.returncode:
            raise RunnerError((stderr or stdout or b"").decode())

        spiders = stdout.decode().splitlines()

        # Note: If the cache is empty, that doesn't mean that this is the project's only version; it simply means that
        # this is the first version called in this Scrapyd process.

        # Evict the return value of version=None calls, since we can't determine whether this version is the default
        # version (in which case we would overwrite it) or not (in which case we would keep it).
        self.cache[project].pop(None, None)
        self.cache[project][version] = spiders
        return spiders

    def delete(self, project, version=None):
        if version is None:
            self.cache.pop(project, None)
        else:
            # Evict the return value of version=None calls, since we can't determine whether this version is the
            # default version (in which case we would pop it) or not (in which case we would keep it).
            self.cache[project].pop(None, None)
            self.cache[project].pop(version, None)


spider_list = SpiderList()


# WebserviceResource
class WsResource(resource.Resource):
    """
    .. versionchanged:: 1.1.0
       Add ``node_name`` to the response in all subclasses.
    """

    json_encoder = json.JSONEncoder()

    def __init__(self, root):
        super().__init__()
        self.root = root

    def render(self, txrequest):
        try:
            data = super().render(txrequest)
        except Exception as e:
            log.failure("")

            if isinstance(e, error.Error):
                txrequest.setResponseCode(int(e.status))

            if self.root.debug:
                return traceback.format_exc().encode()

            message = e.message.decode() if isinstance(e, error.Error) else f"{type(e).__name__}: {e}"
            data = {"status": "error", "message": message}
        else:
            if data is not None:
                data["status"] = "ok"

        if data is None:
            content = b""
        else:
            data["node_name"] = self.root.node_name
            content = self.json_encoder.encode(data).encode() + b"\n"
            txrequest.setHeader("Content-Type", "application/json")

        txrequest.setHeader("Access-Control-Allow-Origin", "*")
        txrequest.setHeader("Access-Control-Allow-Methods", self.methods)
        txrequest.setHeader("Access-Control-Allow-Headers", "X-Requested-With")
        txrequest.setHeader("Content-Length", str(len(content)))
        return content

    def render_OPTIONS(self, txrequest):
        txrequest.setHeader("Allow", self.methods)
        txrequest.setResponseCode(http.NO_CONTENT)

    @functools.cached_property
    def methods(self):
        methods = ["OPTIONS", "HEAD"]
        if hasattr(self, "render_GET"):
            methods.append("GET")
        if hasattr(self, "render_POST"):
            methods.append("POST")
        return ", ".join(methods)

class RawContentResource(resource.Resource):
    def __init__(self, root):
        super().__init__()
        self.root = root

    def render(self, txrequest):
        # try:
        data = super().render(txrequest)
        # except Exception as e:
        #     log.failure("")
        #
        #     if isinstance(e, http.Error):
        #         txrequest.setResponseCode(int(e.status))
        #
        #     if self.root.debug:
        #         return traceback.format_exc().encode()
        #
        #     message = e.message.decode() if isinstance(e, http.Error) else f"{type(e).__name__}: {e}"
        #     txrequest.setResponseCode(http.INTERNAL_SERVER_ERROR)
        #     return message.encode()

        # txrequest.setHeader("Access-Control-Allow-Origin", "*")
        # txrequest.setHeader("Access-Control-Allow-Methods", self.methods)
        # txrequest.setHeader("Access-Control-Allow-Headers", "X-Requested-With")
        return data

    # def render_GET(self, txrequest):
    #     # Ensure the request has a valid file path
    #     file_path = txrequest.args.get(b"path", [b""])[0].decode()
    #     full_path = os.path.join(self.root, file_path)
    #
    #     if not os.path.exists(full_path) or not os.path.isfile(full_path):
    #         txrequest.setResponseCode(http.NOT_FOUND)
    #         return b"File not found"
    #
    #     try:
    #         with open(full_path, "rb") as file:
    #             file_data = file.read()
    #
    #         mime_type, _ = mimetypes.guess_type(full_path)
    #         mime_type = mime_type or "application/octet-stream"
    #
    #         txrequest.setHeader("Content-Type", mime_type)
    #         txrequest.setHeader("Content-Length", str(len(file_data)))
    #
    #         return file_data
    #
    #     except Exception as e:
    #         log.failure(f"Failed to read file: {e}")
    #         txrequest.setResponseCode(http.INTERNAL_SERVER_ERROR)
    #         return b"Error reading file"
    #
    # def render_OPTIONS(self, txrequest):
    #     txrequest.setHeader("Allow", self.methods)
    #     txrequest.setResponseCode(http.NO_CONTENT)
    #
    # @functools.cached_property
    # def methods(self):
    #     methods = ["OPTIONS", "HEAD"]
    #     if hasattr(self, "render_GET"):
    #         methods.append("GET")
    #     return ", ".join(methods)

class JustContentResource(resource.Resource):
    json_encoder = json.JSONEncoder()

    def __init__(self, root):
        super().__init__()
        self.root = root

    def render(self, txrequest):
        try:
            data = super().render(txrequest)
        except Exception as e:  # noqa: BLE001
            log.failure("")

            if isinstance(e, error.Error):
                txrequest.setResponseCode(int(e.status))

            if self.root.debug:
                return traceback.format_exc().encode()

            message = e.message.decode() if isinstance(e, error.Error) else f"{type(e).__name__}: {e}"
            data = {"status": "error", "message": message}

        if data is None:
            content = b""
        else:
            content = self.json_encoder.encode(data).encode() + b"\n"
            txrequest.setHeader("Content-Type", "application/json")

        txrequest.setHeader("Access-Control-Allow-Origin", "*")
        txrequest.setHeader("Access-Control-Allow-Methods", self.methods)
        txrequest.setHeader("Access-Control-Allow-Headers", "X-Requested-With")
        txrequest.setHeader("Content-Length", str(len(content)))
        return content

    def render_OPTIONS(self, txrequest):
        txrequest.setHeader("Allow", self.methods)
        txrequest.setResponseCode(http.NO_CONTENT)

    @functools.cached_property
    def methods(self):
        methods = ["OPTIONS", "HEAD"]
        if hasattr(self, "render_GET"):
            methods.append("GET")
        if hasattr(self, "render_POST"):
            methods.append("POST")
        return ", ".join(methods)


class DaemonStatus(WsResource):
    """
    .. versionadded:: 1.2.0
    """

    def render_GET(self, txrequest):
        return {
            "pending": sum(queue.count() for queue in self.root.poller.queues.values()),
            "running": len(self.root.launcher.processes),
            "finished": len(self.root.launcher.finished),
        }

class SpiderStatus(WsResource):
    @param("project")
    @param("jobid")
    def render_GET(self, txrequest, project, jobid):
        spider = None

        for process in self.root.launcher.processes.values():
            if process.project == project and process.job == jobid:
                spider = process

        if not spider:
            return {
                "code": http.NOT_FOUND,
                "usage": {
                    "cpu": 0,
                    "memory": 0,
                },
                "pid": 0,
                "message": "Spider not found",
            }

        try:
            pid = spider.pid

            if platform.system() == "Linux":
                with open(f'/proc/{pid}/stat', 'r') as f:
                    stats = f.read().split()

                utime = int(stats[13])
                stime = int(stats[14])

                with open(f'/proc/{pid}/status', 'r') as f:
                    memory_info = f.read()
                    for line in memory_info.splitlines():
                        if line.startswith("VmRSS"):
                            memory_usage_kb = int(line.split()[1])
                            break

                memory_usage_mb = memory_usage_kb / 1024
                total_time = utime + stime
                cpu_usage = total_time / (psutil.cpu_count() * 100)

            elif platform.system() == "Windows":
                process = psutil.Process(pid)

                cpu_times = process.cpu_times()
                total_time = cpu_times.user + cpu_times.system

                memory_info = process.memory_info()
                memory_usage_mb = memory_info.rss / (1024 * 1024)

                cpu_usage = total_time / psutil.cpu_count()

            else:
                return {
                    "code": http.NOT_IMPLEMENTED,
                    "usage": {
                        "cpu": 0,
                        "memory": 0,
                    },
                    "pid": pid,
                    "message": f"Unsupported platform: {platform.system()}",
                }

            return {
                "code": http.OK,
                "usage": {
                    "cpu": cpu_usage,
                    "memory": memory_usage_mb,
                },
                "pid": pid,
                "message": "Success",
            }

        except psutil.NoSuchProcess:
            return {
                "code": http.NOT_FOUND,
                "usage": {
                    "cpu": 0,
                    "memory": 0,
                },
                "pid": 0,
                "message": f"Process with pid {pid} not found",
            }
        except Exception as e:
            return {
                "code": http.INTERNAL_SERVER_ERROR,
                "usage": {
                    "cpu": 0,
                    "memory": 0,
                },
                "pid": 0,
                "message": f"Error: {e}",
            }

class SpiderStorage(JustContentResource):
    def render_GET(self, txrequest):
        def get_logs_structure(path):
            logs_structure = []
            for project_name in os.listdir(path):
                project_path = os.path.join(path, project_name)
                if os.path.isdir(project_path):
                    jobs = [
                        job_id for job_id in os.listdir(project_path)
                        if os.path.isdir(os.path.join(project_path, job_id)) and job_id != "general_engine"
                    ]
                    logs_structure.append({
                        "project": project_name,
                        "jobs": jobs
                    })
            return logs_structure

        def get_results_structure(path):
            results_structure = []
            for project_name in os.listdir(path):
                project_path = os.path.join(path, project_name)
                if os.path.isdir(project_path):
                    json_files = [
                        file for file in os.listdir(project_path)
                        if file.endswith(".json") and os.path.isfile(os.path.join(project_path, file))
                    ]
                    results_structure.append({
                        "project": project_name,
                        "data": json_files
                    })
            return results_structure

        logs_path = "logs/"
        results_path = "results/"

        if not os.path.exists(logs_path):
            return {"error": "Logs directory does not exist."}
        if not os.path.exists(results_path):
            return {"error": "Results directory does not exist."}

        logs_structure = get_logs_structure(logs_path)
        results_structure = get_results_structure(results_path)

        return {
            "test": "test",
            "logs": logs_structure,
            "results": results_structure,
        }

class SpiderDownloadLog(RawContentResource):
    @param("project")
    @param("job_id")
    def render_GET(self, txrequest, project, job_id):
        directory_path = f"logs/{project}/{job_id}"

        if not os.path.exists(directory_path) or not os.path.isdir(directory_path):
            txrequest.setResponseCode(http.NOT_FOUND)
            return b"Directory not found"

        try:
            combined_logs = ""
            for root, dirs, files in os.walk(directory_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    with open(file_path, "r", encoding="utf-8") as f:
                        combined_logs += f.read() + "\n"

            combined_logs = combined_logs.strip()

            txrequest.setHeader("Content-Type", "text/plain")
            txrequest.setHeader("Content-Disposition", f'attachment; filename="combined_{job_id}.log"')
            txrequest.setHeader("Content-Length", str(len(combined_logs)))

            return combined_logs.encode("utf-8")
        except Exception as e:
            log.err(f"Failed to read logs: {e}")
            txrequest.setResponseCode(http.INTERNAL_SERVER_ERROR)
            return b"Error reading logs"

class SpiderDownloadResult(RawContentResource):
    @param("project")
    @param("job_id")
    def render_GET(self, txrequest, project, job_id):
        directory_path = f"results/{project}/{job_id}"

        if not os.path.exists(directory_path) or not os.path.isfile(directory_path):
            txrequest.setResponseCode(http.NOT_FOUND)
            return b"File not found"

        base_directory = "results"

        directory_path = os.path.join(base_directory, project, job_id)

        absolute_path = os.path.abspath(directory_path)

        if not absolute_path.startswith(os.path.abspath(base_directory)):
            txrequest.setResponseCode(http.FORBIDDEN)
            # Biar ambigu bang
            return b"File not found"

        if not os.path.exists(absolute_path) or not os.path.isfile(absolute_path):
            txrequest.setResponseCode(http.NOT_FOUND)
            return b"File not found"

        try:

            with open(directory_path, "rb") as f:
                content = f.read()

            txrequest.setHeader("Content-Type", "application/json")
            txrequest.setHeader("Content-Disposition", f'attachment; filename="{job_id}"')
            txrequest.setHeader("Content-Length", str(len(job_id)))

            return content
        except Exception as e:
            log.failure(f"Failed to send file: {e}")
            txrequest.setResponseCode(http.INTERNAL_SERVER_ERROR)
            return b"Error sending results"


class SpiderLogs(JustContentResource):
    @param("project")
    @param("jobid")
    @param("maxlen", required=False)
    def render_GET(self, txrequest, project, jobid, maxlen):
        if maxlen is None:
            maxlen = "40"
        try:
            maxlen = int(maxlen)
        except ValueError:
            maxlen = 40

        log_path = f'logs/{project}/{jobid}/log.log'

        output_lines = []

        try:
            if platform.system() == "Linux":
                f = subprocess.Popen(['tail', '-n', str(maxlen), log_path],
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
                for line in f.stdout:
                    output_lines.append(line.decode().strip())

            elif platform.system() == "Windows":
                with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                    output_lines = lines[-maxlen:]  # Fetch last 'maxlen' lines
                    output_lines = [line.strip() for line in output_lines]

            else:
                return {
                    "code": http.NOT_IMPLEMENTED,
                    "message": f"Unsupported platform: {platform.system()}",
                }

            return list(output_lines)

        except FileNotFoundError:
            return {
                "code": http.NOT_FOUND,
                "message": f"Log file not found: {log_path}",
            }
        except Exception as e:
            return {
                "code": http.INTERNAL_SERVER_ERROR,
                "message": f"Error reading log file: {str(e)}",
            }

class SpiderResults(resource.Resource):
    def __init__(self, root):
        super().__init__()
        self.root = root

    def render(self, txrequest):
        try:
            data = super().render(txrequest)
            return data
        except Exception as e:
            if isinstance(e, error.Error):
                txrequest.setResponseCode(int(e.status))

            if self.root.debug:
                return traceback.format_exc().encode()

            message = e.message.decode() if isinstance(e, error.Error) else f"{type(e).__name__}: {e}"
            data = {"status": "error", "message": message}
            return json.dumps(data).encode("utf-8")

    @param("project")
    @param("configID")
    @param("configName")
    def render_GET(self, txrequest, project, configID, configName):
        try:
            file_path = f"results/{project}/local-{configName}-{configID}-result.json"

            with open(file_path, 'rb') as file:
                content = file.read()

            txrequest.setHeader("Content-Disposition", f"attachment; filename={configName}-{configID}-result")
            txrequest.setHeader("Content-Type", "application/octet-stream")
            txrequest.setHeader("Content-Length", str(len(content)))

            return content
        except FileNotFoundError:
            txrequest.setResponseCode(404)
            return b"File not found"
        except Exception as e:
            txrequest.setResponseCode(500)
            if self.root.debug:
                return traceback.format_exc().encode()
            return f"Error: {e}".encode("utf-8")

class Schedule(WsResource):
    """
    .. versionchanged:: 1.2.0
       Add ``_version`` and ``jobid`` parameters.
    .. versionchanged:: 1.3.0
       Add ``priority`` parameter.
    """

    @param("project")
    @param("spider")
    @param("_version", dest="version", required=False, default=None)
    # See https://github.com/scrapy/scrapyd/pull/215
    @param("jobid", required=False, default=lambda: uuid.uuid1().hex)
    @param("priority", required=False, default=0, type=float)
    @param("setting", required=False, default=list, multiple=True)
    def render_POST(self, txrequest, project, spider, version, jobid, priority, setting):
        if project not in self.root.poller.queues:
            raise error.Error(code=http.OK, message=b"project '%b' not found" % project.encode())

        if version and self.root.eggstorage.get(project, version) == (None, None):
            raise error.Error(code=http.OK, message=b"version '%b' not found" % version.encode())

        spiders = spider_list.get(project, version, runner=self.root.runner)
        if spider not in spiders:
            raise error.Error(code=http.OK, message=b"spider '%b' not found" % spider.encode())

        args = {key.decode(): values[0].decode() for key, values in txrequest.args.items()}
        if version is not None:
            args["_version"] = version

        self.root.scheduler.schedule(
            project,
            spider,
            priority=priority,
            settings=dict(s.split("=", 1) for s in setting),
            _job=jobid,
            **args,
        )
        return {"jobid": jobid}


class Cancel(WsResource):
    @param("project")
    @param("job")
    # Instead of os.name, use sys.platform, which disambiguates Cygwin, which implements SIGINT not SIGBREAK.
    # https://cygwin.com/cygwin-ug-net/kill.html
    # https://github.com/scrapy/scrapy/blob/06f9c28/tests/test_crawler.py#L886
    @param("signal", required=False, default="INT" if sys.platform != "win32" else "BREAK")
    def render_POST(self, txrequest, project, job, signal):
        if project not in self.root.poller.queues:
            raise error.Error(code=http.OK, message=b"project '%b' not found" % project.encode())

        prevstate = None

        if self.root.poller.queues[project].remove(lambda message: message["_job"] == job):
            prevstate = "pending"

        for key, process in list(self.root.launcher.processes.items()):
            if process.project == project and process.job == job:
                process.transport.signalProcess(signal)
                prevstate = "running"
                print(f"your key : {key}")
                self.root.launcher.processes.pop(key)
                process.end_time = datetime.now()
                self.root.launcher.finished.add(process)

        return {"prevstate": prevstate}


class AddVersion(WsResource):
    @param("project")
    @param("version")
    @param("egg", type=bytes)
    def render_POST(self, txrequest, project, version, egg):
        if not zipfile.is_zipfile(BytesIO(egg)):
            raise error.Error(
                code=http.OK, message=b"egg is not a ZIP file (if using curl, use egg=@path not egg=path)"
            )

        self.root.eggstorage.put(BytesIO(egg), project, version)
        self.root.update_projects()

        spiders = spider_list.set(project, version, runner=self.root.runner)
        return {"project": project, "version": version, "spiders": len(spiders)}


class ListProjects(WsResource):
    def render_GET(self, txrequest):
        return {"projects": self.root.scheduler.list_projects()}


class ListVersions(WsResource):
    @param("project")
    def render_GET(self, txrequest, project):
        return {"versions": self.root.eggstorage.list(project)}


class ListSpiders(WsResource):
    """
    .. versionchanged:: 1.2.0
       Add ``_version`` parameter.
    """

    @param("project")
    @param("_version", dest="version", required=False, default=None)
    def render_GET(self, txrequest, project, version):
        if project not in self.root.poller.queues:
            raise error.Error(code=http.OK, message=b"project '%b' not found" % project.encode())

        if version and self.root.eggstorage.get(project, version) == (None, None):
            raise error.Error(code=http.OK, message=b"version '%b' not found" % version.encode())

        return {"spiders": spider_list.get(project, version, runner=self.root.runner)}


class Status(WsResource):
    """
    .. versionadded:: 1.5.0
    """

    @param("job")
    @param("project", required=False)
    def render_GET(self, txrequest, job, project):
        queues = self.root.poller.queues
        if project is not None and project not in queues:
            raise error.Error(code=http.OK, message=b"project '%b' not found" % project.encode())

        result = {"currstate": None}

        for finished in self.root.launcher.finished:
            if (project is None or finished.project == project) and finished.job == job:
                result["currstate"] = "finished"
                return result

        for process in self.root.launcher.processes.values():
            if (project is None or process.project == project) and process.job == job:
                result["currstate"] = "running"
                return result

        for queue_name in queues if project is None else [project]:
            for message in queues[queue_name].list():
                if message["_job"] == job:
                    result["currstate"] = "pending"
                    return result

        return result


class ListJobs(WsResource):
    """
    .. versionchanged:: 1.1.0
       Add ``start_time`` to running jobs in the response.
    .. versionchanged:: 1.2.0
       Add ``pid`` to running jobs in the response.
    .. versionchanged:: 1.3.0
       The ``project`` parameter is optional. Add ``project`` to all jobs in the response.
    .. versionchanged:: 1.4.0
       Add ``log_url`` and ``items_url`` to finished jobs in the response.
    .. versionchanged:: 1.5.0
       Add ``version``, ``settings`` and ``args`` to pending jobs in the response.
    """

    @param("project", required=False)
    def render_GET(self, txrequest, project):
        queues = self.root.poller.queues
        if project is not None and project not in queues:
            raise error.Error(code=http.OK, message=b"project '%b' not found" % project.encode())

        return {
            "pending": [
                {
                    "id": message["_job"],
                    "project": queue_name,
                    "spider": message["name"],
                    "version": message.get("_version"),
                    "settings": message.get("settings", {}),
                    "args": {k: v for k, v in message.items() if k not in ("name", "_job", "_version", "settings")},
                }
                for queue_name in (queues if project is None else [project])
                for message in queues[queue_name].list()
            ],
            "running": [
                {
                    "id": process.job,
                    "project": process.project,
                    "spider": process.spider,
                    "pid": process.pid,
                    "start_time": str(process.start_time),
                    "log_url": self.root.get_log_url(process),
                    "items_url": self.root.get_item_url(process),
                }
                for process in self.root.launcher.processes.values()
                if project is None or process.project == project
            ],
            "finished": [
                {
                    "id": finished.job,
                    "project": finished.project,
                    "spider": finished.spider,
                    "start_time": str(finished.start_time),
                    "end_time": str(finished.end_time),
                    "log_url": self.root.get_log_url(finished),
                    "items_url": self.root.get_item_url(finished),
                }
                for finished in self.root.launcher.finished
                if project is None or finished.project == project
            ],
        }


class DeleteProject(WsResource):
    @param("project")
    def render_POST(self, txrequest, project):
        self._delete_version(project)
        spider_list.delete(project)
        return {}

    def _delete_version(self, project, version=None):
        try:
            self.root.eggstorage.delete(project, version)
        except ProjectNotFoundError as e:
            raise error.Error(code=http.OK, message=b"project '%b' not found" % project.encode()) from e
        except EggNotFoundError as e:
            raise error.Error(code=http.OK, message=b"version '%b' not found" % version.encode()) from e
        else:
            self.root.update_projects()


class DeleteVersion(DeleteProject):
    @param("project")
    @param("version")
    def render_POST(self, txrequest, project, version):
        self._delete_version(project, version)
        spider_list.delete(project, version)
        return {}