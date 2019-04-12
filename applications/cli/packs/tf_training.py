#
# Copyright (c) 2019 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import ast
import os
import re
import shutil
from typing import Tuple, List, Optional
import jinja2

import yaml


from util.logger import initialize_logger
from util.config import FOLDER_DIR_NAME
from util.config import NAUTAConfigMap, NAUTA_NAMESPACE
import packs.common as common
import dpath.util as dutil
from cli_text_consts import PacksTfTrainingTexts as Texts


log = initialize_logger(__name__)


WORK_CNT_PARAM = "workersCount"
P_SERV_CNT_PARAM = "pServersCount"
POD_COUNT_PARAM = "podCount"

NAUTA_REGISTRY_ADDRESS = f'nauta-registry-nginx.{NAUTA_NAMESPACE}:5000'


def update_configuration(run_folder: str, script_location: str,
                         script_parameters: Tuple[str, ...],
                         experiment_name: str,
                         cluster_registry_port: int,
                         pack_type: str,
                         username: str,
                         pack_params: List[Tuple[str, str]] = None,
                         script_folder_location: str = None,
                         env_variables: List[str] = None):
    """
    Updates configuration of a tf-training pack based on paramaters given by a user.

    The following files are modified:
    - Dockerfile - name of a training script is replaced with the one given by a user
                 - all additional files from experiment_folder are copied into an image
                   (excluding files generated by draft)
    - charts/templates/job.yaml - list of arguments is replaces with those given by a user

    :return:
    in case of any errors it throws an exception with a description of a problem
    """
    log.debug("Update configuration - start")

    try:
        modify_values_yaml(run_folder, script_location, script_parameters, pack_params=pack_params,
                           experiment_name=experiment_name, pack_type=pack_type,
                           cluster_registry_port=cluster_registry_port,
                           env_variables=env_variables, username=username)
        modify_dockerfile(experiment_folder=run_folder, script_location=script_location,
                          experiment_name=experiment_name, username=username,
                          script_folder_location=script_folder_location)
    except Exception as exe:
        log.exception("Update configuration - i/o error : {}".format(exe))
        raise RuntimeError(Texts.CONFIG_NOT_UPDATED) from exe

    log.debug("Update configuration - end")


def modify_dockerfile(experiment_folder: str, experiment_name: str, username: str,
                      script_location: str = None, script_folder_location: str = None):
    log.debug("Modify dockerfile - start")
    dockerfile_name = os.path.join(experiment_folder, "Dockerfile")
    dockerfile_temp_name = os.path.join(experiment_folder, "Dockerfile_Temp")
    dockerfile_temp_content = ""

    with open(dockerfile_name, "r") as dockerfile:
        for line in dockerfile:
            if line.startswith("ADD training.py"):
                if script_location or script_folder_location:
                    dockerfile_temp_content = dockerfile_temp_content + f"COPY {FOLDER_DIR_NAME} ."
            elif line.startswith("FROM nauta/tensorflow-py"):
                nauta_config_map = NAUTAConfigMap()
                if line.find('-py2') != -1:
                    tf_image_name = nauta_config_map.py2_image_name
                else:
                    tf_image_name = nauta_config_map.py3_image_name
                tf_image_repository = f'{NAUTA_REGISTRY_ADDRESS}/{tf_image_name}'
                dockerfile_temp_content = dockerfile_temp_content + f'FROM {tf_image_repository}'

            elif line.startswith("FROM nauta/horovod"):
                nauta_config_map = NAUTAConfigMap()
                if line.find('-py2') != -1:
                    horovod_image_name = nauta_config_map.py2_horovod_image_name
                else:
                    horovod_image_name = nauta_config_map.py3_horovod_image_name
                image_repository = f'{NAUTA_REGISTRY_ADDRESS}/{horovod_image_name}'
                dockerfile_temp_content = dockerfile_temp_content + f'FROM {image_repository}'
            else:
                dockerfile_temp_content = dockerfile_temp_content + line

    # Append experiment metadata to Dockerfile - besides enabling access to experiment/user name in experiment's
    # container, it will also make image manifest digest unique, in order to avoid issues with race conditions when
    # image manifest is pushed to docker registry
    dockerfile_temp_content += f'\nENV NAUTA_EXPERIMENT_NAME {experiment_name}\n'
    dockerfile_temp_content += f'\nENV NAUTA_USERNAME {username}\n'

    with open(dockerfile_temp_name, "w") as dockerfile_temp:
        dockerfile_temp.write(dockerfile_temp_content)

    shutil.move(dockerfile_temp_name, dockerfile_name)
    log.debug("Modify dockerfile - end")


def modify_values_yaml(experiment_folder: str, script_location: str, script_parameters: Tuple[str, ...],
                       experiment_name: str, pack_type: str, username: str,
                       cluster_registry_port: int, pack_params: List[Tuple[str, str]],
                       env_variables: List[str]):
    log.debug("Modify values.yaml - start")
    values_yaml_filename = os.path.join(experiment_folder, f"charts/{pack_type}/values.yaml")
    values_yaml_temp_filename = os.path.join(experiment_folder, f"charts/{pack_type}/values_temp.yaml")
    
    with open(values_yaml_filename, "r") as values_yaml_file:
        
        template = jinja2.Template(values_yaml_file.read())

        rendered_values = template.render(NAUTA = {
            'ExperimentName': experiment_name,
            'CommandLine': common.prepare_script_paramaters(script_parameters, script_location),
            'RegistryPort': str(cluster_registry_port),
            'ExperimentImage': f'127.0.0.1:{cluster_registry_port}/{username}/{experiment_name}:latest',
            'ImageRepository': f'127.0.0.1:{cluster_registry_port}/{username}/{experiment_name}:latest'
        })
    
        v = yaml.load(rendered_values)

        workersCount = None
        pServersCount = None

        regex = re.compile(r"^\[.*|^\{.*")  # Regex used for detecting dicts/arrays in pack params
        for key, value in pack_params:
            if re.match(regex, value):
                try:
                    value = ast.literal_eval(value)
                except Exception as e:
                    raise AttributeError(Texts.CANT_PARSE_VALUE.format(value=value, error=e))
            # Handle boolean params
            elif value in {"true", "false"}:
                value = _parse_yaml_boolean(value)
            if key == WORK_CNT_PARAM:
                workersCount = value
            if key == P_SERV_CNT_PARAM:
                pServersCount = value

            dutil.new(v, key, value, '.')

        # setting sum of replicas involved in multinode training if both pServersCount and workersCount are present in
        # the pack or given in the cli
        if (WORK_CNT_PARAM in v or workersCount) and (P_SERV_CNT_PARAM in v or pServersCount):
            number_of_replicas = int(v.get(WORK_CNT_PARAM)) if not workersCount else int(workersCount)
            number_of_replicas += int(v.get(P_SERV_CNT_PARAM)) if not pServersCount else int(pServersCount)
            v[POD_COUNT_PARAM] = number_of_replicas

        if env_variables:
            env_list = []
            for variable in env_variables:
                key, value = variable.split("=")

                one_env_map = {"name": key, "value": value}

                env_list.append(one_env_map)
            if v.get("env"):
                v["env"].extend(env_list)
            else:
                v["env"] = env_list

    with open(values_yaml_temp_filename, "w") as values_yaml_file:
        yaml.dump(v, values_yaml_file)

    shutil.move(values_yaml_temp_filename, values_yaml_filename)
    log.debug("Modify values.yaml - end")


def get_pod_count(run_folder: str, pack_type: str) -> Optional[int]:
    log.debug(f"Getting pod count for Run: {run_folder}")
    values_yaml_filename = os.path.join(run_folder, f"charts/{pack_type}/values.yaml")

    with open(values_yaml_filename, "r") as values_yaml_file:
        values = yaml.load(values_yaml_file)

    pod_count = values.get(POD_COUNT_PARAM)

    log.debug(f"Pod count for Run: {run_folder} = {pod_count}")

    return int(pod_count) if pod_count else None


def _parse_yaml_boolean(value: str) -> bool:
    """
    Parse string according to YAML 1.2 boolean spec:
    http://yaml.org/spec/1.2/spec.html#id2803231
    :param value: YAML boolean string
    :return: bool if string matches YAML boolean spec
    """
    value = str(value)
    if value == 'true':
        return True
    elif value == 'false':
        return False
    else:
        raise ValueError(f'Passed string: {value} is not valid YAML boolean.')
