import uuid
import os
import logging
import shutil
import json
import shlex

from glob import glob
from urllib.request import pathname2url

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core import management, serializers
from django.db import transaction
from django.http import JsonResponse, Http404
from django.shortcuts import render, get_object_or_404, redirect
from django.template.loader import render_to_string
from django.template.response import TemplateResponse

from elasticsearch import Elasticsearch
from elasticsearch_dsl import Search

from guardian.shortcuts import get_objects_for_user, get_perms, assign_perm, remove_perm

from orochi.website.models import (
    Dump,
    Plugin,
    Result,
    ExtractedDump,
    UserPlugin,
)
from orochi.website.forms import (
    DumpForm,
    EditDumpForm,
    ParametersForm,
)

from dask import delayed
from dask.distributed import Client, fire_and_forget
from orochi.utils.volatility_dask_elk import unzip_then_run, run_plugin, get_parameters


##############################
# CHANGELOG
##############################
@login_required
def changelog(request):
    """
    Returns changelog
    """
    changelog_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "CHANGELOG.md"
    )
    with open(changelog_path, "r") as f:
        changelog_content = "<br>".join([x for x in f.readlines()])
    return JsonResponse({"note": changelog_content})


##############################
# PLUGIN
##############################
@login_required
def plugins(request):
    """
    Return list of plugin for selected indexes
    """
    if request.is_ajax():
        indexes = request.GET.getlist("indexes[]")
        # CHECK IF I CAN SEE INDEXES
        dumps = Dump.objects.filter(index__in=indexes)
        for dump in dumps:
            if dump not in get_objects_for_user(request.user, "website.can_see"):
                raise Http404("404")
        results = (
            Result.objects.filter(dump__index__in=indexes)
            .order_by("plugin__name")
            .distinct()
            .values_list("plugin__name", flat=True)
        )
        return render(request, "website/partial_plugins.html", {"results": results})
    else:
        raise Http404("404")


def plugin_f_and_f(dump, plugin, params):
    """
    Fire and forget plugin on dask
    """
    dask_client = Client(settings.DASK_SCHEDULER_URL)
    fire_and_forget(
        dask_client.submit(run_plugin, dump, plugin, settings.ELASTICSEARCH_URL, params)
    )


@login_required
def enable_plugin(request):
    """
    Enable/disable plugin in user settings
    """
    if request.method == "POST":
        plugin = request.POST.get("plugin")
        enable = request.POST.get("enable")
        up = get_object_or_404(UserPlugin, pk=plugin)
        up.automatic = True if enable == "true" else False
        up.save()
        return JsonResponse({"ok": True})


def handle_uploaded_file(index, plugin, f):
    if not os.path.exists("{}/{}/{}".format(settings.MEDIA_ROOT, index, plugin)):
        os.mkdir("{}/{}/{}".format(settings.MEDIA_ROOT, index, plugin))
    with open(
        "{}/{}/{}/{}".format(settings.MEDIA_ROOT, index, plugin, f), "wb+"
    ) as destination:
        for chunk in f.chunks():
            destination.write(chunk)
    return "{}/{}/{}/{}".format(settings.MEDIA_ROOT, index, plugin, f)


@login_required
def plugin(request):
    """
    Prepares for plugin resubmission on selected index with/without parameters
    """
    if request.method == "POST":
        dump = get_object_or_404(Dump, index=request.POST.get("selected_index"))
        if dump not in get_objects_for_user(request.user, "website.can_see"):
            raise Http404("404")
        plugin = get_object_or_404(Plugin, name=request.POST.get("selected_plugin"))
        up = get_object_or_404(UserPlugin, plugin=plugin, user=request.user)

        result = get_object_or_404(Result, dump=dump, plugin=plugin)

        params = {}

        parameters = get_parameters(plugin.name)
        for parameter in parameters:
            if parameter["name"] in request.POST.keys():
                if parameter["mode"] == "list":
                    value = shlex.shlex(request.POST.get(parameter["name"]), posix=True)
                    value.whitespace += ","
                    value.whitespace_split = True
                    value = list(value)
                    if parameter["type"] == int:
                        value = [int(x) for x in value]
                    params[parameter["name"]] = value

                else:
                    if parameter["type"] == bool:
                        params[parameter["name"]] = (
                            True
                            if request.POST.get(parameter["name"]) in ["true", "on"]
                            else False
                        )
                    else:
                        params[parameter["name"]] = request.POST.get(parameter["name"])

        for filename in request.FILES:
            filepath = handle_uploaded_file(
                dump.index, plugin.name, request.FILES.get(filename)
            )
            params[filename] = "file:" + pathname2url(filepath)

        # REMOVE OLD DATA
        es_client = Elasticsearch([settings.ELASTICSEARCH_URL])
        es_client.indices.delete(
            "{}_{}".format(dump.index, plugin.name.lower()), ignore=[400, 404]
        )
        eds = ExtractedDump.objects.filter(result=result)
        eds.delete()

        result.result = 0
        request.description = None
        result.parameter = params
        result.save()

        plugin_f_and_f(dump, plugin, params)
        return JsonResponse(
            {
                "ok": True,
                "plugin": plugin.name,
                "name": request.POST.get("selected_name"),
            }
        )
    else:
        raise Http404("404")


@login_required
def parameters(request):
    """
    Get parameters from volatility api, returns form
    """
    data = dict()

    if request.method == "POST":
        form = ParametersForm(data=request.POST, dynamic_fields=parameters)
        if form.is_valid():
            data["form_is_valid"] = True
        else:
            data["form_is_valid"] = False
    else:
        data = {
            "selected_plugin": request.GET.get("selected_plugin"),
            "selected_index": request.GET.get("selected_index"),
            "selected_name": request.GET.get("selected_name"),
        }

        parameters = get_parameters(data["selected_plugin"])
        form = ParametersForm(initial=data, dynamic_fields=parameters)

    context = {"form": form}
    data["html_form"] = render_to_string(
        "website/partial_params.html",
        context,
        request=request,
    )
    return JsonResponse(data)


##############################
# RESULTS
##############################
@login_required
def analysis(request):
    """
    Get and trasform results for selected plugin on selected indexes
    """
    if request.is_ajax():
        es_client = Elasticsearch([settings.ELASTICSEARCH_URL])

        # GET DATA
        indexes = request.GET.getlist("indexes[]")
        plugin = request.GET.get("plugin")

        # GET PLUGIN INFO
        plugin = get_object_or_404(Plugin, name=plugin)

        # GET DICT OF COLOR AND CHECK PERMISSIONS
        dumps = Dump.objects.filter(index__in=indexes)
        colors = {}
        for dump in dumps:
            if dump not in get_objects_for_user(request.user, "website.can_see"):
                raise Http404("404")
            colors[dump.index] = dump.color

        # GET ALL RESULTS
        results = Result.objects.select_related("dump", "plugin").filter(
            plugin__name=plugin, dump__index__in=indexes
        )

        # GET ALL EXTRACTED DUMP DUMP
        ex_dumps = {
            x["path"]: x
            for x in ExtractedDump.objects.filter(result__in=results).values(
                "path", "sha256", "clamav", "vt_report", "pk"
            )
        }

        # SEARCH FOR ITEMS AND KEEP INDEX
        indexes_list = [
            f"{res.dump.index}_{res.plugin.name.lower()}"
            for res in results
            if res.result == 2
        ]

        # GENERATE NOTE TO SHOW ON TOP
        note = [
            {
                "dump_name": res.dump.name,
                "plugin": res.plugin.name,
                "disabled": res.plugin.disabled,
                "index": res.dump.index,
                "result": res.get_result_display(),
                "description": res.description,
                "color": """<svg class="bd-placeholder-img rounded mr-2" width="20" height="20" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid slice" focusable="false" role="img">
                        <rect width="100%" height="100%" fill="{}"></rect>
                        </svg>
                    """.format(
                    colors[res.dump.index]
                ),
            }
            for res in results
        ]

        data = []
        if indexes_list:
            s = Search(using=es_client, index=indexes_list).extra(size=10000)
            result = s.execute()
            # ANNOTATE RESULTS WITH INDEX NAME
            info = [
                (
                    hit.to_dict(),
                    hit.meta.index.split("_")[0],
                    hit.meta.index.split("_")[1],
                )
                for hit in result
            ]

            row_colors = {
                "Created Date": "#FF0000",
                "Modified Date": "#00FF00",
                "Accessed Date": "#0000FF",
                "Changed Date": "#FFFF00",
            }

            for item, item_index, plugin_index in info:
                if item_index != ".kibana":

                    if "File output" in item.keys():

                        glob_path = None

                        if plugin_index == "windows.dlllist.dlllist":
                            glob_path = "{}/{}/{}/pid.{}.{}.*.{}.dmp".format(
                                settings.MEDIA_ROOT,
                                item_index,
                                plugin.name,
                                item["PID"],
                                item["Name"],
                                item["Base"],
                            )
                        elif plugin_index in (
                            "windows.malfind.malfind",
                            "linux.malfind.malfind",
                            "mac.malfind.malfind",
                        ):
                            glob_path = "{}/{}/{}/pid.{}.vad.{}-{}.dmp".format(
                                settings.MEDIA_ROOT,
                                item_index,
                                plugin.name,
                                item["PID"],
                                item["Start VPN"],
                                item["End VPN"],
                            )
                        elif plugin_index in [
                            "windows.modscan.modscan",
                            "windows.modules.modules",
                        ]:
                            glob_path = "{}/{}/{}/{}.{}.{}.dmp".format(
                                settings.MEDIA_ROOT,
                                item_index,
                                plugin.name,
                                item["Path"].split("\\")[-1]
                                if item["Name"]
                                else "UnreadbleDLLName",
                                item["Offset"],
                                item["Base"],
                            )
                        elif plugin_index == "windows.pslist.pslist":
                            glob_path = "{}/{}/{}/pid.{}.*.dmp".format(
                                settings.MEDIA_ROOT,
                                item_index,
                                plugin.name,
                                item["PID"],
                            )
                        elif plugin_index == "windows.registry.hivelist.hivelist":
                            glob_path = "{}/{}/{}/registry.*.{}.hive".format(
                                settings.MEDIA_ROOT,
                                item_index,
                                plugin.name,
                                item["Offset"],
                            )

                        if glob_path:
                            try:
                                path = glob(glob_path)[0]
                                down_path = path.replace(
                                    settings.MEDIA_ROOT, settings.MEDIA_URL.rstrip("/")
                                )

                                item["download"] = (
                                    '<a href="{}">⬇️</a>'.format(down_path)
                                    if os.path.exists(path)
                                    else ""
                                )

                                item["sha256"] = ex_dumps.get(path, {}).get(
                                    "sha256", ""
                                )

                                if plugin.clamav_check:
                                    value = ex_dumps.get(path, {}).get("clamav", "")
                                    item["clamav"] = value if value else ""

                                if plugin.vt_check:
                                    vt_data = ex_dumps.get(path, {}).get(
                                        "vt_report", {}
                                    )

                                    item[
                                        "vt_report"
                                    ] = '<dl class="row">{}</dl>'.format(
                                        "".join(
                                            [
                                                '<dt class="col-sm-3 {}">{}</dt><dd class="col-sm-9 {}">{}</dd>'.format(
                                                    "text-danger"
                                                    if k == "malicious"
                                                    else "",
                                                    k,
                                                    "text-danger"
                                                    if k == "malicious"
                                                    else "",
                                                    v,
                                                )
                                                for k, v in vt_data.items()
                                                if v != 0
                                            ]
                                        )
                                        if vt_data
                                        else ""
                                    )

                                if plugin.regipy_check:
                                    value = ex_dumps.get(path, {}).get("pk", None)
                                    item["regipy_report"] = (
                                        """<a href="/json_view/{}" target="_blank">📝</a>""".format(
                                            value
                                        )
                                        if value
                                        else ""
                                    )

                            except IndexError:
                                item["download"] = ""
                                item["sha256"] = ""
                                if plugin.clamav_check:
                                    item["clamav"] = ""
                                if plugin.vt_check:
                                    item["vt_report"] = ""
                                if plugin.regipy_check:
                                    item["regipy_report"] = ""

                    # TIMELINER PAINT ROW BY TIPE
                    if plugin_index == "timeliner.timeliner":

                        columns = [x for x in item.keys() if x.find("Date") != -1]
                        other_columns = [x for x in item.keys() if x.find("Date") == -1]

                        parsed = False
                        for column in columns:
                            if item[column]:
                                parsed = True
                                row = {"__children": []}
                                row["Date"] = item[column]
                                row["Type"] = column
                                row["row_color"] = row_colors[column]
                                for oc in other_columns:
                                    row[oc] = item[oc]
                                row.update(
                                    {
                                        "color": """<svg class="bd-placeholder-img rounded mr-2" width="20" height="20" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid slice" focusable="false" role="img"><rect width="100%" height="100%" fill="{}"></rect></svg>""".format(
                                            colors[item_index]
                                        )
                                    }
                                )
                                data.append(row)

                        if not parsed:
                            row = {"__children": []}
                            row["Date"] = None
                            row["Type"] = None
                            row["row_color"] = None
                            for oc in other_columns:
                                row[oc] = item[oc]
                            row.update(
                                {
                                    "color": """<svg class="bd-placeholder-img rounded mr-2" width="20" height="20" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid slice" focusable="false" role="img"><rect width="100%" height="100%" fill="{}"></rect></svg>""".format(
                                        colors[item_index]
                                    )
                                }
                            )
                            data.append(row)

                    else:
                        item.update(
                            {
                                "color": """<svg class="bd-placeholder-img rounded mr-2" width="20" height="20" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid slice" focusable="false" role="img"><rect width="100%" height="100%" fill="{}"></rect></svg>""".format(
                                    colors[item_index]
                                )
                            }
                        )

                        data.append(item)

        if plugin.name in ["windows.pstree.PsTree", "linux.pstree.PsTree"]:

            def change_keys(obj):
                if isinstance(obj, dict):
                    new = {}
                    for k, v in obj.items():
                        if k == "__children" and v != []:
                            new["children"] = change_keys(v)
                        elif k == "PID":
                            new["text"] = v
                        elif not v:
                            new.setdefault("data", {})[k] = "-"
                        else:
                            new.setdefault("data", {})[k] = v

                elif isinstance(obj, list):
                    new = []
                    for v in obj:
                        new.append(change_keys(v))
                else:
                    return obj
                return new

            new_data = [change_keys(item) for item in data]
            if len(new_data) > 0:
                columns = [{"header": "PID", "value": "text", "width": 500}] + [
                    {"header": x, "value": x, "width": 500}
                    for x in new_data[0].get("data", {}).keys()
                ]
            else:
                columns = None

            context = {
                "data": json.dumps(new_data),
                "columns": json.dumps(columns),
                "note": note,
                "tree": True,
                "empty": False if new_data else True,
            }
        else:
            context = {
                "data": data,
                "note": note,
                "children": False,
                "tree": False,
            }
        return render(request, "website/partial_analysis.html", context)
    else:
        raise Http404("404")


##############################
# SPECIAL VIEWER
##############################
@login_required
def json_view(request, pk):
    """
    Render json for hive dump
    """
    ed = get_object_or_404(ExtractedDump, pk=pk)
    if ed.result.dump not in get_objects_for_user(request.user, "website.can_see"):
        raise Http404("404")

    context = {"data": json.dumps(ed.reg_array["values"])}

    return render(request, "website/json_view.html", context)


@login_required
def diff_view(request, index_a, index_b, plugin):
    """
    Compare json views
    """
    obj_a = get_object_or_404(Dump, index=index_a)
    obj_b = get_object_or_404(Dump, index=index_b)
    es_client = Elasticsearch([settings.ELASTICSEARCH_URL])
    search_a = (
        Search(using=es_client, index=["{}_{}".format(index_a, plugin.lower())])
        .extra(size=10000)
        .execute()
    )
    info_a = json.dumps([hit.to_dict() for hit in search_a])

    search_b = (
        Search(using=es_client, index=["{}_{}".format(index_b, plugin.lower())])
        .extra(size=10000)
        .execute()
    )
    info_b = json.dumps([hit.to_dict() for hit in search_b])

    context = {"info_a": info_a, "info_b": info_b}

    return render(request, "website/diff_view.html", context)


##############################
# DUMP
##############################
@login_required
def index(request):
    """
    List of available indexes
    """
    context = {
        "dumps": get_objects_for_user(request.user, "website.can_see")
        .values_list("index", "name", "color", "operating_system", "author")
        .order_by("-created_at"),
    }
    return TemplateResponse(request, "website/index.html", context)


@login_required
def edit(request):
    """
    Edit index information
    """
    data = dict()

    # GET OBJECT AND CHECK PERMISSIONS
    if request.method == "POST":
        dump = get_object_or_404(Dump, index=request.POST.get("index"))
    else:
        dump = get_object_or_404(Dump, index=request.GET.get("index"))

    if dump not in get_objects_for_user(request.user, "website.can_see"):
        raise Http404("404")

    # GET OTHERS USERS
    auth_users = [
        user.pk
        for user in get_user_model().objects.all()
        if "can_see" in get_perms(user, dump) and user != request.user
    ]

    if request.method == "POST":
        form = EditDumpForm(
            data=request.POST,
            instance=dump,
            initial={"authorized_users": auth_users},
            user=request.user,
        )

        if dumpform.is_valid():
            dump = dumpform.save()

            # REFRESH PERMISSIONS
            for user_pk in dumpform.cleaned_data["authorized_users"]:
                user = get_user_model().objects.get(pk=user_pk)
                if user.pk not in auth_users:
                    assign_perm(
                        "can_see",
                        user,
                        dump,
                    )
            for user_pk in auth_users:
                if user_pk not in form.cleaned_data["authorized_users"]:
                    user = get_user_model().objects.get(pk=user_pk)
                    remove_perm("can_see", user, dump)

            data["form_is_valid"] = True
            data["dumps"] = render_to_string(
                "website/partial_indices.html",
                {
                    "dumps": [
                        x
                        for x in get_objects_for_user(request.user, "website.can_see")
                        .values_list(
                            "index", "color", "name", "operating_system", "author"
                        )
                        .order_by("-created_at")
                    ]
                },
                request=request,
            )
        else:
            data["form_is_valid"] = False
    else:
        form = EditDumpForm(
            instance=dump, initial={"authorized_users": auth_users}, user=request.user
        )

    context = {"form": form}
    data["html_form"] = render_to_string(
        "website/partial_edit.html",
        context,
        request=request,
    )
    return JsonResponse(data)


def index_f_and_f(dump_pk, user_pk):
    """
    Run all plugin for a new index on dask
    """
    dask_client = Client(settings.DASK_SCHEDULER_URL)
    fire_and_forget(dask_client.submit(unzip_then_run, dump_pk, user_pk))


@login_required
def create(request):
    """
    Manage new index creation
    """
    data = dict()

    if request.method == "POST":
        form = DumpForm(data=request.POST)
        if form.is_valid():
            with transaction.atomic():
                dump = form.save(commit=False)
                dump.author = request.user
                dump.upload = form.cleaned_data["upload"]
                dump.index = str(uuid.uuid1())
                dump.save()
                form.delete_temporary_files()
                os.mkdir("{}/{}".format(settings.MEDIA_ROOT, dump.index))
                data["form_is_valid"] = True

                # for each plugin enabled and for that os I create a result
                # if the user selected that for automation, run it immediately on dask
                Result.objects.bulk_create(
                    [
                        Result(
                            plugin=up.plugin,
                            dump=dump,
                            result=5 if not up.automatic else 0,
                        )
                        for up in UserPlugin.objects.filter(
                            plugin__operating_system__in=[
                                dump.operating_system,
                                "Other",
                            ],
                            user=request.user,
                            plugin__disabled=False,
                        )
                    ]
                )

                transaction.on_commit(lambda: index_f_and_f(dump.pk, request.user.pk))

            # Return the new list of available indexes
            data["form_is_valid"] = True
            data["dumps"] = render_to_string(
                "website/partial_indices.html",
                {
                    "dumps": [
                        x
                        for x in get_objects_for_user(request.user, "website.can_see")
                        .values_list(
                            "index", "color", "name", "operating_system", "author"
                        )
                        .order_by("-created_at")
                    ]
                },
                request=request,
            )
        else:
            data["form_is_valid"] = False
    else:
        form = DumpForm()

    context = {"form": form}
    data["html_form"] = render_to_string(
        "website/partial_create.html",
        context,
        request=request,
    )
    return JsonResponse(data)


@login_required
def delete(request):
    """
    Delete an index
    """
    if request.is_ajax():
        es_client = Elasticsearch([settings.ELASTICSEARCH_URL])
        index = request.GET.get("index")
        dump = Dump.objects.get(index=index)
        if dump not in get_objects_for_user(request.user, "website.can_see"):
            Http404("404")
        dump.delete()
        es_client.indices.delete(index=f"{index}*", ignore=[400, 404])
        shutil.rmtree("{}/{}".format(settings.MEDIA_ROOT, dump.index))
        return JsonResponse({"ok": True}, safe=False)


##############################
# ADMIN
##############################
def update_plugins(request):
    """
    Run management command to update plugins
    """
    if request.user.is_superuser:
        management.call_command("plugins_sync", verbosity=0)
        messages.add_message(request, messages.INFO, "Sync Plugin done")
        return redirect("/admin")
    raise Http404("404")


def update_symbols(request):
    """
    Run management command to update symbols
    """
    if request.user.is_superuser:
        management.call_command("symbols_sync", verbosity=0)
        messages.add_message(request, messages.INFO, "Sync Symbols done")
        return redirect("/admin")
    raise Http404("404")
