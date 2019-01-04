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

import click
from tabulate import tabulate

from util.aliascmd import AliasCmd
from cli_state import common_options, pass_state, State
from version import VERSION
from util.config import DLS4EConfigMap
from util.exceptions import KubernetesError
from util.logger import initialize_logger
from util.system import handle_error
from cli_text_consts import VersionCmdTexts as Texts


logger = initialize_logger(__name__)

# Timeout for version check request in seconds. This request is repeated 3 times.
PLATFORM_VERSION_REQUEST_TIMEOUT = 10


@click.command(help=Texts.HELP, short_help=Texts.HELP, cls=AliasCmd, alias='v', options_metavar='[options]')
@common_options(verify_dependencies=False, verify_config_path=False)
@pass_state
def version(state: State):
    """ Returns the version of the installed dlsctl application. """
    platform_version = Texts.INITIAL_PLATFORM_VERSION
    error_msg = ""
    platform_version_fail = False
    try:
        platform_version = DLS4EConfigMap(config_map_request_timeout=PLATFORM_VERSION_REQUEST_TIMEOUT).platform_version
    except KubernetesError:
        error_msg = Texts.KUBECTL_INT_ERROR_MSG
        platform_version_fail = True
    except Exception:
        error_msg = Texts.OTHER_ERROR_MSG
        platform_version_fail = True

    version_table = [[Texts.TABLE_APP_ROW_NAME, VERSION],
                     [Texts.TABLE_PLATFORM_ROW_NAME, platform_version]]

    click.echo(tabulate(version_table,
                        headers=Texts.TABLE_HEADERS,
                        tablefmt="orgtbl"))

    if platform_version_fail:
        handle_error(logger, error_msg, error_msg, add_verbosity_msg=state.verbosity == 0)
