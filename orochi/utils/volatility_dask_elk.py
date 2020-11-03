import io
import os
import time
import attr
import uuid
import traceback
import hashlib
import json
import tempfile
import pathlib
import datetime
import logging

import pyclamd
import virustotal3.core
from regipy.registry import RegistryHive
from regipy.plugins.utils import dump_hive_to_json

from glob import glob
from typing import Any, List, Tuple, Dict, Optional, Union
from urllib.request import pathname2url

import volatility.plugins
import volatility.symbols
from volatility import framework
from volatility.cli.text_renderer import (
    JsonRenderer,
    format_hints,
    quoted_optional,
    hex_bytes_as_text,
    optional,
    display_disassembly,
)
from volatility.framework.configuration import requirements

from volatility.framework import (
    automagic,
    contexts,
    constants,
    exceptions,
    interfaces,
    plugins,
)

from zipfile import ZipFile, is_zipfile
from elasticsearch import Elasticsearch, helpers
from elasticsearch_dsl import Search

from orochi.website.models import (
    Dump,
    Plugin,
    Result,
    ExtractedDump,
    UserPlugin,
    Service,
    OS_ARCHITECTURE,
    OS_FAMILY,
)

from dask import delayed
from distributed import get_client, secede, rejoin

from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings

from guardian.shortcuts import get_users_with_perms

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


class MuteProgress(object):
    """
    Mutes progress for volatility plugin
    """

    def __init__(self):
        self._max_message_len = 0

    def __call__(self, progress: Union[int, float], description: str = None):
        pass


def file_handler_class_factory(output_dir, file_list):
    class NullFileHandler(io.BytesIO, interfaces.plugins.FileHandlerInterface):
        """Null FileHandler that swallows files whole without consuming memory"""

        def __init__(self, preferred_name: str):
            interfaces.plugins.FileHandlerInterface.__init__(self, preferred_name)
            super().__init__()

        def writelines(self, lines):
            """Dummy method"""
            pass

        def write(self, data):
            """Dummy method"""
            return len(data)

    class OrochiFileHandler(interfaces.plugins.FileHandlerInterface):
        def __init__(self, filename: str):
            fd, self._name = tempfile.mkstemp(suffix=".vol3", prefix="tmp_")
            self._file = io.open(fd, mode="w+b")
            interfaces.plugins.FileHandlerInterface.__init__(self, filename)
            for item in dir(self._file):
                if not item.startswith("_") and not item in [
                    "closed",
                    "close",
                    "mode",
                    "name",
                ]:
                    setattr(self, item, getattr(self._file, item))

        def __getattr__(self, item):
            return getattr(self._file, item)

        @property
        def closed(self):
            return self._file.closed

        @property
        def mode(self):
            return self._file.mode

        @property
        def name(self):
            return self._file.name

        def getvalue(self) -> bytes:
            """Mimic a BytesIO object's getvalue parameter"""
            # Opens the file new so we're not trying to do IO on a closed file
            this_file = open(self._name, mode="rb")
            return this_file.read()

        def delete(self):
            self.close()
            os.remove(self._name)

        def close(self):
            """Closes and commits the file (by moving the temporary file to the correct name"""
            # Don't overcommit
            if self._file.closed:
                return

            file_list.append(self)

    if output_dir:
        return OrochiFileHandler
    return NullFileHandler


class ReturnJsonRenderer(JsonRenderer):
    """
    Custom json renderer that doesn't write json on disk but returns it with errors if present
    """

    _type_renderers = {
        format_hints.HexBytes: quoted_optional(hex_bytes_as_text),
        format_hints.Hex: optional(lambda x: "0x{:x}".format(x)),
        interfaces.renderers.Disassembly: quoted_optional(display_disassembly),
        datetime.datetime: lambda x: x.isoformat()
        if not isinstance(x, interfaces.renderers.BaseAbsentValue)
        else None,
        "default": lambda x: x,
    }

    def render(self, grid: interfaces.renderers.TreeGrid):
        final_output = ({}, [])

        def visitor(
            node: Optional[interfaces.renderers.TreeNode],
            accumulator: Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]],
        ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
            # Nodes always have a path value, giving them a path_depth of at least 1, we use max just in case
            acc_map, final_tree = accumulator
            node_dict = {"__children": []}
            for column_index in range(len(grid.columns)):
                column = grid.columns[column_index]
                renderer = self._type_renderers.get(
                    column.type, self._type_renderers["default"]
                )
                data = renderer(list(node.values)[column_index])
                if isinstance(data, interfaces.renderers.BaseAbsentValue):
                    data = None
                node_dict[column.name] = data
            if node.parent:
                acc_map[node.parent.path]["__children"].append(node_dict)
            else:
                final_tree.append(node_dict)
            acc_map[node.path] = node_dict
            return (acc_map, final_tree)

        error = grid.populate(visitor, final_output, fail_on_errors=False)
        return final_output[1], error


def gendata(index, plugin_name, result):
    """
    Elastic bulk insert generator
    """
    for item in result:
        yield {
            "_index": index,
            # "_type": plugin_name,
            "_id": uuid.uuid4(),
            "_source": item,
        }


def sha256_checksum(filename, block_size=65536):
    """
    Generate sha256 for filename
    """
    sha256 = hashlib.sha256()
    with open(filename, "rb") as f:
        for block in iter(lambda: f.read(block_size), b""):
            sha256.update(block)
    return sha256.hexdigest()


def get_parameters(plugin):
    """
    Obtains parameters list from volatility plugin
    """
    ctx = contexts.Context()
    failures = framework.import_files(volatility.plugins, True)
    plugin_list = framework.list_plugins()
    params = []
    if plugin in plugin_list:
        for requirement in plugin_list[plugin].get_requirements():
            additional = {}
            additional["optional"] = requirement.optional
            additional["name"] = requirement.name
            if isinstance(requirement, requirements.URIRequirement):
                additional["mode"] = "single"
                additional["type"] = "file"
            elif isinstance(
                requirement, interfaces.configuration.SimpleTypeRequirement
            ):
                additional["mode"] = "single"
                additional["type"] = requirement.instance_type
            elif isinstance(
                requirement,
                volatility.framework.configuration.requirements.ListRequirement,
            ):
                additional["mode"] = "list"
                additional["type"] = requirement.element_type
            elif isinstance(
                requirement,
                volatility.framework.configuration.requirements.ChoiceRequirement,
            ):
                additional["type"] = str
                additional["mode"] = "single"
                additional["choices"] = requirement.choices
            else:
                continue
            params.append(additional)
    return params


def run_vt(result_pk, filepath):
    """
    Runs virustotal on filepath
    """
    try:
        vt = Service.objects.get(name=1)
        vt_files = virustotal3.core.Files(vt.key, proxies=vt.proxy)
        try:
            vt_report = json.loads(
                json.dumps(
                    vt_files.info_file(sha256_checksum(filepath))
                    .get("data", {})
                    .get("attributes", {})
                    .get("last_analysis_stats", {})
                )
            )
        except virustotal3.errors.VirusTotalApiError as excp:
            vt_report = None
    except ObjectDoesNotExist:
        vt_report = {"error": "Service not configured"}

    ed = ExtractedDump.objects.get(result__pk=result_pk, path=filepath)
    ed.vt_report = vt_report
    ed.save()


def run_regipy(result_pk, filepath):
    """
    Runs regipy on filepath
    """
    try:
        registry_hive = RegistryHive(filepath)
        reg_json = registry_hive.recurse_subkeys(registry_hive.root, as_json=True)
        root = {"values": [attr.asdict(entry) for entry in reg_json]}
        root = json.loads(json.dumps(root).replace(r"\u0000", ""))
    except Exception as e:
        logging.error(e)
        root = {}

    ed = ExtractedDump.objects.get(result__pk=result_pk, path=filepath)
    ed.reg_array = root
    ed.save()


def send_to_ws(dump, result, plugin_name):
    """
    Notifies plugin result to websocket
    """

    colors = {1: "green", 2: "green", 3: "orange", 4: "red"}

    users = get_users_with_perms(dump, only_with_perms_in=["can_see"])

    channel_layer = get_channel_layer()
    for user in users:
        async_to_sync(channel_layer.group_send)(
            "chat_{}".format(user.pk),
            {
                "type": "chat_message",
                "message": "{}||Plugin <b>{}</b> on dump <b>{}</b> ended<br>Status: <b style='color:{}'>{}</b>".format(
                    datetime.datetime.now().strftime("%d/%m/%Y %H:%M"),
                    plugin_name,
                    dump.name,
                    colors[result.result],
                    result.get_result_display(),
                ),
            },
        )


def run_plugin(dump_obj, plugin_obj, params=None):
    """
    Execute a single plugin on a dump with optional params.
    If success data are sent to elastic.
    """
    logging.debug("[dump {} - plugin {}] start".format(dump_obj.pk, plugin_obj.pk))
    try:
        ctx = contexts.Context()
        constants.PARALLELISM = constants.Parallelism.Off
        failures = framework.import_files(volatility.plugins, True)
        automagics = automagic.available(ctx)
        plugin_list = framework.list_plugins()
        json_renderer = ReturnJsonRenderer
        seen_automagics = set()
        for amagic in automagics:
            if amagic in seen_automagics:
                continue
            seen_automagics.add(amagic)
        plugin = plugin_list.get(plugin_obj.name)
        base_config_path = "plugins"
        file_name = os.path.abspath(dump_obj.upload.path)
        single_location = "file:" + pathname2url(file_name)
        ctx.config["automagic.LayerStacker.single_location"] = single_location
        automagics = automagic.choose_automagic(automagics, plugin)

        # LOCAL DUMPS REQUIRES FILES
        local_dump = plugin_obj.local_dump

        # ADD PARAMETERS, AND IF LOCAL DUMP ENABLE ADD DUMP TRUE BY DEFAULT
        plugin_config_path = interfaces.configuration.path_join(
            base_config_path, plugin.__name__
        )
        if params:
            # ADD PARAMETERS TO PLUGIN CONF
            for k, v in params.items():
                extended_path = interfaces.configuration.path_join(
                    plugin_config_path, k
                )
                ctx.config[extended_path] = v

                if k == "dump" and v == True:
                    # IF DUMP TRUE HAS BEEN PASS IT'LL DUMP LOCALLY
                    local_dump = True

        if not params and local_dump:
            # IF ADMIN SET LOCAL DUMP ADD DUMP TRUE AS PARAMETER
            extended_path = interfaces.configuration.path_join(
                plugin_config_path, "dump"
            )
            ctx.config[extended_path] = True

        logging.debug(
            "[dump {} - plugin {}] params: {}".format(
                dump_obj.pk, plugin_obj.pk, ctx.config
            )
        )

        file_list = []
        if local_dump:
            # IF PARAM/ADMIN DUMP CREATE FILECONSUMER
            local_path = "{}/{}/{}".format(
                settings.MEDIA_ROOT, dump_obj.index, plugin_obj.name
            )
            if not os.path.exists(local_path):
                os.mkdir(local_path)
            file_handler = file_handler_class_factory(
                output_dir=local_path, file_list=file_list
            )
        else:
            file_handler = file_handler_class_factory(
                output_dir=None, file_list=file_list
            )

        try:
            # RUN PLUGIN
            constructed = plugins.construct_plugin(
                ctx,
                automagics,
                plugin,
                base_config_path,
                MuteProgress(),
                file_handler,
            )
        except exceptions.UnsatisfiedException as excp:
            # LOG UNSATISFIED ERROR
            result = Result.objects.get(plugin=plugin_obj, dump=dump_obj)
            result.result = 3
            result.description = "\n".join(
                [
                    excp.unsatisfied[config_path].description
                    for config_path in excp.unsatisfied
                ]
            )
            result.save()
            send_to_ws(dump_obj, result, plugin_obj.name)

            logging.error(
                "[dump {} - plugin {}] unsatisfied".format(dump_obj.pk, plugin_obj.pk)
            )

            return 0
        try:
            runned_plugin = constructed.run()
        except Exception as excp:
            # LOG GENERIC ERROR [VOLATILITY]
            fulltrace = traceback.TracebackException.from_exception(excp).format(
                chain=True
            )
            result = Result.objects.get(plugin=plugin_obj, dump=dump_obj)
            result.result = 4
            result.description = "\n".join(fulltrace)
            result.save()
            send_to_ws(dump_obj, result, plugin_obj.name)
            logging.error(
                "[dump {} - plugin {}] generic error".format(dump_obj.pk, plugin_obj.pk)
            )
            return 0

        # RENDER OUTPUT IN JSON AND PUT IT IN ELASTIC
        json_data, error = json_renderer().render(runned_plugin)

        if len(json_data) > 0:

            # IF DUMP STORE FILE ON DISK
            if local_dump and file_list:
                for file_id in file_list:
                    output_path = "{}/{}".format(local_path, file_id.preferred_filename)
                    with open(output_path, "wb") as f:
                        f.write(file_id.getvalue())

                ## RUN CLAMAV ON ALL FOLDER
                if plugin_obj.clamav_check:
                    cd = pyclamd.ClamdUnixSocket()
                    match = cd.multiscan_file(local_path)
                    match = {} if not match else match
                else:
                    match = {}

                result = Result.objects.get(plugin=plugin_obj, dump=dump_obj)

                # BULK CREATE EXTRACTED DUMP FOR EACH DUMPED FILE
                ed = ExtractedDump.objects.bulk_create(
                    [
                        ExtractedDump(
                            result=result,
                            path="{}/{}".format(local_path, file_id.preferred_filename),
                            sha256=sha256_checksum(
                                "{}/{}".format(local_path, file_id.preferred_filename)
                            ),
                            clamav=(
                                match[
                                    "{}/{}".format(
                                        local_path,
                                        file_id.preferred_filename,
                                    )
                                ][1]
                                if "{}/{}".format(
                                    local_path, file_id.preferred_filename
                                )
                                in match.keys()
                                else None
                            ),
                        )
                        for file_id in file_list
                    ]
                )

                ## RUN VT AND REGIPY AS DASK SUBTASKS
                if plugin_obj.vt_check or plugin_obj.regipy_check:
                    dask_client = get_client()
                    secede()
                    tasks = []
                    for file_id in file_list:
                        task = dask_client.submit(
                            run_vt if plugin_obj.vt_check else run_regipy,
                            result.pk,
                            "{}/{}".format(local_path, file_id.preferred_filename),
                        )
                        tasks.append(task)
                    results = dask_client.gather(tasks)
                    rejoin()

            es = Elasticsearch(
                [settings.ELASTICSEARCH_URL],
                request_timeout=60,
                timeout=60,
                max_retries=10,
                retry_on_timeout=True,
            )
            helpers.bulk(
                es,
                gendata(
                    "{}_{}".format(dump_obj.index, plugin_obj.name.lower()),
                    plugin_obj.name,
                    json_data,
                ),
            )
            # EVERYTHING OK
            result = Result.objects.get(plugin=plugin_obj, dump=dump_obj)
            result.result = 2
            result.description = error
            result.save()

            logging.debug(
                "[dump {} - plugin {}] sent to elastic".format(
                    dump_obj.pk, plugin_obj.pk
                )
            )
        else:
            # OK BUT EMPTY
            result = Result.objects.get(plugin=plugin_obj, dump=dump_obj)
            result.result = 1
            result.description = error
            result.save()

            logging.debug(
                "[dump {} - plugin {}] empty".format(dump_obj.pk, plugin_obj.pk)
            )
        send_to_ws(dump_obj, result, plugin_obj.name)
        return 0

    except Exception as excp:
        # LOG GENERIC ERROR [ELASTIC]
        fulltrace = traceback.TracebackException.from_exception(excp).format(chain=True)
        result = Result.objects.get(plugin=plugin_obj, dump=dump_obj)
        result.result = 4
        result.description = "\n".join(fulltrace)
        result.save()
        send_to_ws(dump_obj, result, plugin_obj.name)
        logging.error(
            "[dump {} - plugin {}] generic error".format(dump_obj.pk, plugin_obj.pk)
        )
        return 0


def check_os(result):
    es_client = Elasticsearch([settings.ELASTICSEARCH_URL])
    s = Search(
        using=es_client,
        index="{}_{}".format(result.dump.index, result.plugin.name.lower()),
    )
    banners = [hit.to_dict().get("Banner", None) for hit in s.execute()]
    logging.error("banners: {}".format(banners))
    for hit in banners:
        if hit.find("Linux version") != -1:
            info = hit.strip().split()[2]
            kernel, *_, architecture = info.split("-")
            if architecture not in [x for (x, _) in OS_ARCHITECTURE]:
                architecture = None
            if hit.lower().find("debian") != -1:
                family = "Debian"
            elif hit.lower().find("ubuntu") != -1:
                family = "Ubuntu"
            else:
                result.dump.description = hit.strip()
            return {"family": family, "architecture": architecture, "kernel": kernel}
        else:
            logging.error("[dump {}] symbol hit: {}".format(result.dump.pk, hit))
    return {"family": None, "architecture": None, "kernel": None}


def unzip_then_run(dump_pk, user_pk):

    dump = Dump.objects.get(pk=dump_pk)
    logging.debug("[dump {}] Processing".format(dump_pk))

    # Unzip file is zipped
    if is_zipfile(dump.upload.path):
        with ZipFile(dump.upload.path, "r") as zipObj:
            objs = zipObj.namelist()
            extract_path = pathlib.Path(dump.upload.path).parent

            # zip must contain one file with a memory dump
            if len(objs) == 1:
                newpath = zipObj.extract(objs[0], extract_path)

            # or a vmem + vmss + vmsn
            elif any([x.lower().endswith(".vmem") for x in objs]):
                zipObj.extractall(extract_path)
                for x in objs:
                    if x.endswith(".vmem"):
                        newpath = os.path.join(extract_path, x)

            else:
                # zip is unvalid
                logging.error("[dump {}] Invalid zipped dump data".format(dump_pk))
                dump.status = 4
                dump.save()
                return
    else:
        newpath = dump.upload.path

    dump.upload.name = newpath
    dump.save()

    # check symbols using banners
    if dump.operating_system == "Linux":
        banner = dump.result_set.get(plugin__name="banners.Banners")
        if banner:
            run_plugin(dump, banner.plugin)
            time.sleep(1)
            os = check_os(banner)
            dump.family = os["family"]
            dump.architecture = os["architecture"]
            dump.kernel = os["kernel"]
            logging.error("[dump {}] guessed symbols {}".format(dump_pk, os))
            dump.save()

    dask_client = get_client()
    secede()
    tasks = []
    tasks_list = (
        dump.result_set.all()
        if dump.operating_system != "Linux"
        else dump.result_set.exclude(plugin__name="banners.Banners")
    )
    for result in tasks_list:
        if result.result != 5:
            task = dask_client.submit(run_plugin, dump, result.plugin)
            tasks.append(task)
    results = dask_client.gather(tasks)
    logging.debug("[dump {}] tasks submitted".format(dump_pk))
    rejoin()
    dump.status = 2
    dump.save()
    logging.debug("[dump {}] processing terminated".format(dump_pk))
