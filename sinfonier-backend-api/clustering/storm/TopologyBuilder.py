import json
import os
import uuid
import re
import codecs
import xml.etree.cElementTree as ET

from clustering.mongo.MongoHandler import MongodbFactory
from clustering.mvn.Mvn import Mvn
from config.config import conf
from error.ErrorHandler import ModuleWriteException, ModuleErrorCompilingException, \
    PathException, GeneratePomFileException, TopologyWriteException
from logger.Logger import logger
from utils.SinfonierConstants import Topology as TopologyConsts, Module as ModuleConsts, ModuleVersions as ModuleVersionConst
from utils.compiler.CompilerHandler import MvnDependency, Compiler, PomFile


class TopologyBuilder(object):
    def __init__(self, topology_model):

        self.topology = topology_model
        self.module_versions_fields = {}
        # create a workspace
        self.base_path = Compiler.generate_basic_workspace()
        # code file path
        file_name = os.path.join(self.base_path, 'topology.json')

        try:
            try:
                with codecs.open(file_name, 'w', 'utf-8') as text_file:
                    t_dict = self.build_backend_json(self.topology)
                    t_json = json.dumps(t_dict)
                    logger.info(t_json)
                    text_file.write(t_json)
            except OSError as e:
                raise TopologyWriteException(e.message)

            self.generate_pom_file(base_path=self.base_path, name=self.topology[TopologyConsts.FIELD_NAME], version='1.0',
                                   dependencies=self.build_dependencies())

        except ModuleWriteException as Ex:
            raise Ex
        except ModuleErrorCompilingException as Ex:
            raise Ex

    def build_dependencies(self):
        dependencies, ids = [], dict()
        for module in self.topology[TopologyConsts.FIELD_CONFIG][TopologyConsts.FIELD_MODULES]:
            _module_id = module[ModuleConsts.FIELD_MODULE_ID]
            _module_version_code = module[ModuleConsts.FIELD_VERSION_CODE]

            if not ids.get(_module_id):
                ids.setdefault(_module_id, [_module_version_code])
            elif ids.get(_module_id) and not ids.get(_module_id).__contains__(_module_version_code):
                _module_versions_codes = ids.get(_module_id)
                _module_versions_codes.append(_module_version_code)
                ids.setdefault(_module_id, _module_versions_codes)

        _modules = MongodbFactory.get_modules(ids.keys())

        for module in _modules:
            if module is not None:
                _versions = ids.get(module.get(ModuleVersionConst.FIELD_ID).__str__())
                dependencies.extend(
                    map(lambda v: MvnDependency(group_id='com.sinfonier', artifact_id=module[ModuleConsts.FIELD_NAME], version=str(v)),
                        _versions))
                m_libraries = module.get(ModuleVersionConst.FIELD_LIBRARIES)

                if m_libraries is not None:
                    m_deps = list(
                        map(Compiler.build_mvn_dependency_from_url, [l[ModuleVersionConst.FIELD_LIBRARY_URL] for l in m_libraries]))
                    dependencies.extend(m_deps)
        return dependencies

    @staticmethod
    def generate_pom_file(base_path, name, version, dependencies=list()):
        """
        Generate a pom file for compiling with mvn
        NOTE: the package it's only temporally
        :raise GeneratePomFileException
        :type base_path: str
        :type name: str
        :type version: int
        :type dependencies: list
        :return: path to pom.xml file
        """

        if not os.path.exists(base_path):
            raise PathException('The path: ' + base_path + ' not exists and it\'s required')

        pom_file_base = Compiler.generate_pom_file(base_path, name, version, dependencies)
        pom_base = PomFile(pom_file_base)
        pom_tree = pom_base.get_base()

        for child in pom_tree.getroot():
            if 'build' in child.tag:
                for build_child in list(child):
                    if 'plugins' in build_child.tag:
                        build_child.append(
                            ET.fromstring('<plugin><artifactId>maven-assembly-plugin</artifactId><configuration><archive>'
                                          '<manifest><mainClass>com.sinfonier.DynamicTopology</mainClass></manifest></archive>'
                                          '<descriptorRefs><descriptorRef>jar-with-dependencies</descriptorRef></descriptorRefs>'
                                          '</configuration></plugin>'))

                    if 'resources' in build_child.tag:
                        build_child.append(
                            ET.fromstring('<resource><directory>${basedir}</directory><includes><include>topology.json</include>'
                                          '</includes></resource>'))

        try:
            pom_tree.write(pom_file_base)
        except OSError as Ex:
            raise GeneratePomFileException(Ex.message)

        return pom_file_base

    def build_jar_with_dependencies(self, quiet_mode=False):
        result = Mvn.build_topology_jar(self.base_path, quiet_mode)
        logger.debug('Command output: ' + str(result.log()))
        result = Mvn.parse_process_result(result)
        if result:
            self.jar_name = self.topology[TopologyConsts.FIELD_NAME] + '-1.0-jar-with-dependencies.jar'
        return result

    def remove(self):
        if self.base_path:
            Compiler.remove_dir(self.base_path)

    def get_topology_json(self):
        # get topology mongo json
        topologyMongoInfo = MongodbFactory.get_topology(self.topology.FIELD_ID)
        return self.build_backend_json(topologyMongoInfo)

    def build_backend_json(self, topologyInfo):

        backend_json = dict()

        if "config" in topologyInfo and all(key in topologyInfo["config"] for key in ("modules", "wires", "stormProperties")):

            backend_json["properties"] = self.validate_topology_properties(topologyInfo["config"]["stormProperties"])

            modules = self.get_modules_info(topologyInfo["config"]["modules"],topologyInfo["config"].get("topologyProperties"))
            normalizedWires = self.normalizeWiresSourceTarget(topologyInfo["config"]["wires"])
            modulesWithWires = self.set_modules_wires(modules, normalizedWires)

            backend_json["builderConfig"] = dict()
            backend_json["builderConfig"] = self.list_of_modules_by_type(modulesWithWires)

        else:
            # Throw error
            pass

        return backend_json

    def validate_topology_properties(self, topoProperties):
        extraField = topoProperties.get('extraConfiguration')
        if extraField is not None:
            extraField = extraField.strip()
            for item in extraField.split("\n"):
                if "=" in item:
                    extraParam = {item.split("=")[0]: item.split("=")[1]}
                    topoProperties.update(extraParam)

            topoProperties.pop("extraConfiguration")
        topoProperties['name'] = self.topology[TopologyConsts.FIELD_NAME]

        if topoProperties.get(TopologyConsts.PROP_MAX_SPOUT_PENDING) is not None:
            topoProperties['topology.max.spout.pending'] = int(topoProperties.pop(TopologyConsts.PROP_MAX_SPOUT_PENDING))
        else:
            topoProperties['topology.max.spout.pending'] = conf.TOPOLOGY_MAX_SPOUT_PENDING

        if topoProperties.get(TopologyConsts.PROP_NUM_WORKERS) is not None:
            topoProperties['topology.workers'] = int(topoProperties.pop(TopologyConsts.PROP_NUM_WORKERS))
        else:
            topoProperties['topology.workers'] = conf.TOPOLOGY_NUM_WORKERS

        if topoProperties.get(TopologyConsts.PROP_TIMEOUT) is not None:
            topoProperties['topology.message.timeout.secs'] = int(topoProperties.pop(TopologyConsts.PROP_TIMEOUT))
        else:
            topoProperties['topology.message.timeout.secs'] = conf.TOPOLOGY_MESSAGE_TIMEOUT

        # ToDo Validation

        return topoProperties

    def get_modules_info(self, modules, topoProperties):
        modules_info = list()
        for module in modules:
            if all(key in module for key in ("type", "language", "name", "value")):

                if module["type"] == "operator":
                    module["type"] = "bolt"

                mod_info = dict()
                mod_info["type"] = module["type"]

                if module["language"] == "python":
                    mod_info["class"] = "com.sinfonier.{module_type}s.Python{module_type_caps}Wrapper".format(
                        module_type=module["type"], module_type_caps=module["type"].title())
                    mod_info["pyscript"] = module["name"].lower() + ".py"
                elif module["language"] == "java":
                    mod_info["class"] = "com.sinfonier.{module_type}s.{module_name}".format(module_type=module["type"],
                                                                                            module_name=module["name"])

                mod_info["language"] = module["language"]
                mod_info["abstractionId"] = "{module_name}_{random_id}".format(module_name=module["name"], random_id=uuid.uuid4())
                mod_info["parallelism"] = module["parallelisms"] if "parallelisms" in module else "1"
                mod_info["language"] = module["language"]
                mod_info["sources"] = list()
                mod_info["params"] = dict()

                for param, val in module["value"].items():
                    value = self.replace_value(module["module_version_id"],param,val,topoProperties)
                    if type(value) is list:
                        mod_info["params"][param] = list()
                        list_type = self.get_list_type(module["value"][param])

                        for item in value:
                            if list_type == "string":
                                mod_info["params"][param].append(item)
                            elif list_type == "keyvalue":
                                mod_info["params"][param].append({item[0]: item[1]})
                            elif list_type == "keyvaluedefault":
                                mod_info["params"][param].append({item[0]: item[1], "default": item[2]})
                    elif type(value) is float:
                        val = str(value)
                        if val.endswith('.0'):
                            val = val[:-2]
                        mod_info["params"][param] = val
                    elif type(value) is unicode:
                        mod_info["params"][param] = value
                    else:
                        mod_info["params"][param] = str(value)

                modules_info.append(mod_info)

            else:
                # Throw error
                pass
        return modules_info

    def normalizeWiresSourceTarget(self, wires):

        for wire in wires:
            if "src" in wire and "terminal" in wire["src"] and wire["src"]["terminal"] not in ["out", "yes", "no"]:
                 wire["src"], wire["tgt"] = wire["tgt"], wire["src"]

        return wires

    def set_modules_wires(self, modules, wires):
        for wire in wires:
            newwire = dict()

            if all(key in wire for key in ("tgt", "src")):
                if "terminal" in wire["tgt"] and wire["tgt"]["terminal"] != "in[]":
                    globalVariableKey, globalVariableValue = modules[wire["src"]["moduleId"]]["params"].popitem()
                    target_var = modules[wire["tgt"]["moduleId"]]["params"][globalVariableKey]
                    if target_var == "[wired]" or target_var == "None":
                        modules[wire["tgt"]["moduleId"]]["params"][globalVariableKey] = globalVariableValue
                    break
                if "terminal" in wire["src"] and wire["src"]["terminal"] != "out":
                    newwire["streamId"] = wire["src"]["terminal"]

                newwire["sourceId"] = modules[wire["src"]["moduleId"]]["abstractionId"]
                newwire["grouping"] = "shuffle"

                modules[wire["tgt"]["moduleId"]]["sources"].append(newwire)

            else:
                # Throw error
                pass

        return modules

    def list_of_modules_by_type(self, t_modules):
        modules = dict()
        modules["spouts"] = list()
        modules["bolts"] = list()
        modules["drains"] = list()

        for module in t_modules:
            if module["type"] in ["spout", "bolt", "drain"]:
                modules[module["type"] + "s"].append(module)

        return modules

    def get_list_type(self, list_values):
        if all(isinstance(item, basestring) for item in list_values):
            return "string"
        elif all(isinstance(item, list_values) and len(item) == 2 for item in list_values):
            return "keyvalue"
        elif all(isinstance(item, list_values) and len(item) == 3 for item in list_values):
            return "keyvaluedefault"
        else:
            return "error"

    def replace_value(self,module_version_id,param, value,properties):
        if properties is None:
            return value
        if isinstance(value, basestring):
            pattern = re.compile("^\[\$(.+)\]$")
            res = pattern.match(value)
            if res is not None:
                value = properties.get(res.groups()[0])
                field_type = self.get_module_version_fields(module_version_id)[param]["type"]
                if field_type == "number":
                    return float(value)
                elif field_type == "integer":
                    return int(value)
                elif field_type == "bool":
                    return bool(value)
                elif field_type == "list":
                    return value.split(',')
                elif field_type == "url":
                    return value
        elif isinstance(value,list):
            res = []
            for item in value:
                value = self.replace_value(module_version_id,param,item,properties);
                if isinstance(value,list):
                    res.extend(value)
                else:
                    res.append(value)
            return res
        return value

    def get_module_version_fields(self,version_id):
        module_version_fields = self.module_versions_fields.get(version_id)
        if module_version_fields is None:
            module_version = MongodbFactory.get_module_version(version_id)
            module_version_fields = {}
            for field in module_version["fields"]:
                module_version_fields[field["name"]] = field
            self.module_versions_fields[version_id] = module_version_fields
        return module_version_fields