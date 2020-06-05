
from collections import deque
import logging
import os
import pathlib

import pandas as pd

from PyDSS.pyLogger import getLoggerTag
from PyDSS.unitDefinations import unit_info
from PyDSS.dataset_buffer import DatasetBuffer
from PyDSS.exceptions import InvalidConfiguration, InvalidParameter
from PyDSS.export_list_reader import ExportListReader, StoreValuesType
from PyDSS.utils.dataframe_utils import write_dataframe
from PyDSS.utils.utils import dump_data
from PyDSS.value_storage import ValueContainer, ValueByNumber, DatasetPropertyType


logger = logging.getLogger(__name__)


class ResultData:
    """Exports data to files."""

    METADATA_FILENAME = "metadata.json"
    INDICES_BASENAME = "indices"

    def __init__(self, options, system_paths, dss_objects,
                 dss_objects_by_class, dss_buses, dss_solver, dss_command,
                 dss_instance):
        if options["Logging"]["Pre-configured logging"]:
            logger_tag = __name__
        else:
            logger_tag = getLoggerTag(options)
        self._logger = logging.getLogger(logger_tag)
        self._dss_solver = dss_solver
        self._results = {}
        self._buses = dss_buses
        self._objects_by_element = dss_objects
        self._objects_by_class = dss_objects_by_class
        self.system_paths = system_paths
        self._elements = []
        self._options = options

        self._dss_command = dss_command
        self._dss_instance = dss_instance
        self._start_day = options["Project"]["Start Day"]
        self._end_day = options["Project"]["End Day"]
        self._time_dataset = None
        self._frequency_dataset = None
        self._mode_dataset = None
        self._simulation_mode = []
        self._hdf_store = None
        self._scenario = options["Project"]["Active Scenario"]
        self._base_scenario = options["Project"]["Active Scenario"]
        self._export_format = options["Exports"]["Export Format"]
        self._export_compression = options["Exports"]["Export Compression"]
        self._export_iteration_order = options["Exports"]["Export Iteration Order"]
        self._max_chunk_bytes = options["Exports"]["HDF Max Chunk Bytes"]
        self._export_dir = os.path.join(
            self.system_paths["Export"],
            options["Project"]["Active Scenario"],
        )
        # Use / because this is used in HDFStore
        self._export_relative_dir = f"Exports/" + options["Project"]["Active Scenario"]
        self._store_frequency = False
        self._store_mode = False
        self.CurrentResults = {}
        if options["Project"]["Simulation Type"] == "Dynamic" or \
                options["Frequency"]["Enable frequency sweep"]:
            self._store_frequency = True
            self._store_mode = True

        if options["Exports"]["Export Mode"] == "byElement":
            raise InvalidParameter(
                "Export Mode 'byElement' is not supported by ResultData"
            )

        pathlib.Path(self._export_dir).mkdir(parents=True, exist_ok=True)

        export_list_filename = os.path.join(
            system_paths["ExportLists"],
            "Exports.toml",
        )
        if not os.path.exists(export_list_filename):
            export_list_filename = os.path.join(
                system_paths["ExportLists"],
                "ExportMode-byClass.toml",
            )
        self._export_list = ExportListReader(export_list_filename)
        self._create_exports()

    def _create_exports(self):
        elements = {}  # element name to ElementData
        for elem_class in self._export_list.list_element_classes():
            if elem_class == "Buses":
                objs = self._buses
            elif elem_class in self._objects_by_class:
                objs = self._objects_by_class[elem_class]
            else:
                continue
            for name, obj in objs.items():
                if not obj.Enabled:
                    continue
                for prop in self._export_list.iter_export_properties(elem_class=elem_class):
                    if prop.custom_function is None and not obj.IsValidAttribute(prop.name):
                        raise InvalidParameter(f"{name} / {prop.name} cannot be exported")
                    if prop.should_store_name(name):
                        if name not in elements:
                            elements[name] = ElementData(
                                name,
                                obj,
                                max_chunk_bytes=self._max_chunk_bytes,
                                options=self._options
                            )
                        elements[name].append_property(prop)
                        self._logger.debug("Store %s %s name=%s", elem_class, prop.name, name)

        self._elements = elements.values()

    def InitializeDataStore(self, hdf_store, num_steps, MC_scenario_number=None):
        if MC_scenario_number is not None:
            self._scenario = self._base_scenario + f"_MC{MC_scenario_number}"
        self._hdf_store = hdf_store
        self._time_dataset = DatasetBuffer(
            hdf_store=hdf_store,
            path=f"Exports/{self._scenario}/Timestamp",
            max_size=num_steps,
            dtype=float,
            columns=("Timestamp",),
            max_chunk_bytes=self._max_chunk_bytes
        )
        self._frequency_dataset = DatasetBuffer(
            hdf_store=hdf_store,
            path=f"Exports/{self._scenario}/Frequency",
            max_size=num_steps,
            dtype=float,
            columns=("Frequency",),
            max_chunk_bytes=self._max_chunk_bytes
        )
        self._mode_dataset = DatasetBuffer(
            hdf_store=hdf_store,
            path=f"Exports/{self._scenario}/Mode",
            max_size=num_steps,
            dtype="S10",
            columns=("Mode",),
            max_chunk_bytes=self._max_chunk_bytes
        )

        for element in self._elements:
            element.initialize_data_store(hdf_store, self._scenario, num_steps)

    def UpdateResults(self):
        self.CurrentResults.clear()

        timestamp = self._dss_solver.GetDateTime().timestamp()
        self._time_dataset.write_value(timestamp)
        self._frequency_dataset.write_value(self._dss_solver.getFrequency())
        self._mode_dataset.write_value(self._dss_solver.getMode())

        for elem in self._elements:
            data = elem.append_values(timestamp)
            self.CurrentResults.update(data)
        return self.CurrentResults

    def ExportResults(self, fileprefix=""):
        self.FlushData()
        for element in self._elements:
            element.export_sums()

        metadata = {
            "event_log": None,
            "element_info_files": [],
        }

        if self._options["Exports"]["Export Event Log"]:
            self._export_event_log(metadata)
        if self._options["Exports"]["Export Elements"]:
            self._export_elements(metadata)
        if self._options["Exports"]["Export PV Profiles"]:
            self._export_pv_profiles()

        filename = os.path.join(self._export_dir, self.METADATA_FILENAME)
        dump_data(metadata, filename, indent=4)
        self._logger.info("Exported metadata to %s", filename)
        self._hdf_store = None

    def FlushData(self):
        for dataset in (self._time_dataset, self._frequency_dataset, self._mode_dataset):
            dataset.flush_data()
        for element in self._elements:
            element.flush_data()

    def _export_event_log(self, metadata):
        # TODO: move to a base class
        event_log = "event_log.csv"
        file_path = os.path.join(self._export_dir, event_log)
        if os.path.exists(file_path):
            os.remove(file_path)

        orig = os.getcwd()
        os.chdir(self._export_dir)
        try:
            cmd = "Export EventLog {}".format(event_log)
            out = self._dss_command(cmd)
            if out != event_log:
                raise Exception(f"Failed to export EventLog:  {out}")
            self._logger.info("Exported OpenDSS event log to %s", out)
            metadata["event_log"] = self._export_relative_dir + f"/{event_log}"
        finally:
            os.chdir(orig)

    def _export_elements(self, metadata):
        dss = self._dss_instance
        exports = (
            # TODO: opendssdirect does not provide a function to export Bus information.
            ("CapacitorsInfo", dss.Capacitors.Count, dss.utils.capacitors_to_dataframe),
            ("FusesInfo", dss.Fuses.Count, dss.utils.fuses_to_dataframe),
            ("GeneratorsInfo", dss.Generators.Count, dss.utils.generators_to_dataframe),
            ("IsourceInfo", dss.Isource.Count, dss.utils.isource_to_dataframe),
            ("LinesInfo", dss.Lines.Count, dss.utils.lines_to_dataframe),
            ("LoadsInfo", dss.Loads.Count, dss.utils.loads_to_dataframe),
            ("MetersInfo", dss.Meters.Count, dss.utils.meters_to_dataframe),
            ("MonitorsInfo", dss.Monitors.Count, dss.utils.monitors_to_dataframe),
            ("PVSystemsInfo", dss.PVsystems.Count, dss.utils.pvsystems_to_dataframe),
            ("ReclosersInfo", dss.Reclosers.Count, dss.utils.reclosers_to_dataframe),
            ("RegControlsInfo", dss.RegControls.Count, dss.utils.regcontrols_to_dataframe),
            ("RelaysInfo", dss.Relays.Count, dss.utils.relays_to_dataframe),
            ("SensorsInfo", dss.Sensors.Count, dss.utils.sensors_to_dataframe),
            ("TransformersInfo", dss.Transformers.Count, dss.utils.transformers_to_dataframe),
            ("VsourcesInfo", dss.Vsources.Count, dss.utils.vsources_to_dataframe),
            ("XYCurvesInfo", dss.XYCurves.Count, dss.utils.xycurves_to_dataframe),
            # TODO This can be very large. Consider making it configurable.
            #("LoadShapeInfo", dss.LoadShape.Count, dss.utils.loadshape_to_dataframe),
        )

        for filename, count_func, get_func in exports:
            if count_func() > 0:
                df = get_func()
                # Always record in CSV format for readability.
                # There are also warning messages from PyTables because the
                # data may contain strings.
                fname = filename + ".csv"
                relpath = os.path.join(self._export_relative_dir, fname)
                filepath = os.path.join(self._export_dir, fname)
                write_dataframe(df, filepath)
                metadata["element_info_files"].append(relpath)
                self._logger.info("Exported %s information to %s.", filename, filepath)

        self._export_transformers(metadata)

    def _export_transformers(self, metadata):
        dss = self._dss_instance
        df_dict = {"Transformer": [], "HighSideConnection": [], "NumPhases": []}

        dss.Circuit.SetActiveClass("Transformer")
        flag = dss.ActiveClass.First()
        while flag > 0:
            name = dss.CktElement.Name()
            df_dict["Transformer"].append(name)
            df_dict["HighSideConnection"].append(dss.Properties.Value("conns").split("[")[1].split(",")[0].strip(" ").lower())
            df_dict["NumPhases"].append(dss.CktElement.NumPhases())
            flag = dss.ActiveClass.Next()

        df = pd.DataFrame.from_dict(df_dict)

        relpath = os.path.join(self._export_relative_dir, "TransformersPhaseInfo.csv")
        filepath = os.path.join(self._export_dir, "TransformersPhaseInfo.csv")
        write_dataframe(df, filepath)
        metadata["element_info_files"].append(relpath)
        self._logger.info("Exported transformer phase information to %s", filepath)

    def _export_pv_profiles(self):
        dss = self._dss_instance
        pv_systems = self._objects_by_class.get("PVSystems")
        if pv_systems is None:
            raise InvalidConfiguration("PVSystems are not exported")

        pv_infos = []
        profiles = set()
        for full_name, obj in pv_systems.items():
            profile_name = obj.GetParameter("yearly").lower()
            profiles.add(profile_name)
            pv_infos.append({
                "irradiance": obj.GetParameter("irradiance"),
                "name": full_name,
                "pmpp": obj.GetParameter("pmpp"),
                "load_shape_profile": profile_name,
            })

        pmult_sums = {}
        dss.LoadShape.First()
        while True:
            name = dss.LoadShape.Name().lower()
            if name in profiles:
                pmult_sums[name] = sum(dss.LoadShape.PMult())
            if dss.LoadShape.Next() == 0:
                break

        for pv_info in pv_infos:
            profile = pv_info["load_shape_profile"]
            pv_info["load_shape_pmult_sum"] = pmult_sums[profile]

        data = {"pv_systems": pv_infos}
        filename = os.path.join(self._export_dir, "pv_profiles.json")
        dump_data(data, filename, indent=2)
        self._logger.info("Exported PV profile information to %s", filename)

    @staticmethod
    def get_units(prop, index=None):
        units = unit_info.get(prop)
        if units is None:
            raise InvalidParameter(f"no units are stored for {prop}")

        if isinstance(units, dict):
            if index is None:
                raise InvalidParameter(f"index must be provided for {prop}")
            if index == 0:
                return units["E"]
            if index == 1:
                return units["O"]
            raise InvalidParameter("index must be 0 or 1")

        return units

    def max_num_bytes(self):
        """Return the maximum number of bytes the container could hold.

        Returns
        -------
        int

        """
        total = 0
        for element in self._elements:
            total += element.max_num_bytes()
        return total


class ElementData:
    """Stores all property data for an element."""
    def __init__(self, name, obj, max_chunk_bytes, options, scenario=None, hdf_store=None):
        self._properties = []
        self._name = name
        self._obj = obj
        self._data = {}  # Containers for properties per time point on disk.
        self._circular_buf = {}  # Keeps last n values in memory for averages.
        self._sums = {}  # Keeps running sums in memory.
        self._num_steps = None
        self._scenario = scenario
        self._hdf_store = hdf_store
        self._max_chunk_bytes = max_chunk_bytes
        self._options = options
        self._step_number = 1

    @staticmethod
    def _data_key(prop):
        return (prop.elem_class, prop.name)

    def initialize_data_store(self, hdf_store, scenario, num_steps):
        self._hdf_store = hdf_store
        self._num_steps = num_steps
        self._scenario = scenario
        # Reset these for MonteCarlo simulations.
        for key in self._data:
            self._data[key] = None
        self._step_number = 1

    def append_property(self, prop):
        self._properties.append(prop)
        key = self._data_key(prop)
        self._data[key] = None
        if prop.store_values_type == StoreValuesType.SUM:
            self._sums[key] = None
        elif prop.store_values_type == StoreValuesType.MOVING_AVERAGE:
            self._circular_buf[key] = _CircularBufferHelper(prop)

    def append_values(self, timestamp):
        curr_data = {}
        for prop in self.properties:
            if not prop.should_sample_value(self._step_number):
                continue
            if prop.custom_function is None:
                value = self._obj.GetValue(prop.name, convert=True)
            else:
                value = prop.custom_function(self._obj, timestamp, self._step_number, self._options)
            key = self._data_key(prop)
            if prop.store_values_type == StoreValuesType.SUM:
                if self._sums[key] is None:
                    self._sums[key] = value
                else:
                    self._sums[key] += value
                # These values are not written to disk until the end.
                continue
            if not prop.should_store_value(value.value):
                continue
            suffix = ""
            if prop.store_values_type == StoreValuesType.MOVING_AVERAGE:
                buf = self._circular_buf[key]
                buf.append(value.value)
                if buf.should_store():
                    suffix = "Avg"
                    value.set_element_property(prop.name + suffix)
                    value.set_value(buf.average())
                else:
                    continue
            if len(value.make_columns()) > 1:
                for column, val in zip(value.make_columns(), value.value):
                    curr_data[column] = val
            else:
                curr_data[value.make_columns()[0]] = value.value
            if self._data[key] is None:
                path = f"Exports/{self._scenario}/{prop.elem_class}/{self._name}/{prop.name}{suffix}"
                self._data[key] = ValueContainer(
                    value,
                    self._hdf_store,
                    path,
                    prop.get_max_size(self._num_steps),
                    dataset_property_type=prop.get_dataset_property_type(),
                    max_chunk_bytes=self._max_chunk_bytes,
                    store_timestamp=prop.should_store_timestamp(),
                )

            self._data[key].append(value, timestamp=timestamp)
        self._step_number += 1
        return curr_data

    def export_sums(self):
        """Export properties stored as sums to disk."""
        for key, value in self._sums.items():
            path = f"Exports/{self._scenario}/{key[0]}/{self._name}/{key[1]}Sum"
            container = ValueContainer(
                value,
                self._hdf_store,
                path,
                1,
                max_chunk_bytes=self._max_chunk_bytes,
                dataset_property_type=DatasetPropertyType.SUM,
            )
            container.append(value)
            container.flush_data()

    def flush_data(self):
        """Flush any outstanding data to disk."""
        for container in self._data.values():
            if container is None:
                continue
            container.flush_data()

    def max_num_bytes(self):
        """Return the maximum number of bytes the element could store.

        Returns
        -------
        int

        """
        total = 0
        for container in self._data.values():
            if container is None:
                logger.debug("max_num_bytes is unknown; no value has been collected yet")
                continue
            total += container.max_num_bytes()
        return total

    @property
    def name(self):
        return self._name

    @property
    def properties(self):
        return self._properties[:]


class _CircularBufferHelper:
    def __init__(self, prop):
        self._buf = deque(maxlen=prop.window_size)
        self._count = 0
        self._store_interval = prop.moving_average_store_interval

    def append(self, val):
        self._buf.append(val)
        self._count += 1
        if self._count == self._store_interval:
            self._count = 0

    def average(self):
        assert self._buf
        if isinstance(self._buf[0], list):
            return pd.DataFrame(self._buf).mean().values
        return sum(self._buf) / len(self._buf)

    def should_store(self):
        return self._count == 0
