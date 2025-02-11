import time
import os
import glob
import copy
import re
import yaml
import validators
import numpy as np
import pandas as pd
import threading
import concurrent.futures

import sqlalchemy as sa
from src.database import Session
from src.parameters import (
    Parameters,
    convert_data_type,
    normalize_data_types,
    get_class_from_data_type,
)
from src.source import Source, get_source_identifiers
from src.dataset import DatasetMixin, RawPhotometry, Lightcurve
from src.catalog import Catalog
from src.utils import help_with_class, help_with_object

lock = threading.Lock()


class ParsObservatory(Parameters):
    """
    Base class for individual observatories' parameters.
    When inheriting, make sure to lock the attributes by
    setting _enforce_no_new_attrs=True and then
    calling load_then_update().
    """

    # add more keywords (lower case!) to prevent them from being read as keywords to the general parameters
    # allowed_obs_names = ['demo', 'ztf', 'tess', 'kepler', 'k2', 'gaia', 'panstarrs', 'lsst', 'des', 'sdss']
    allowed_obs_names = []

    @classmethod
    def add_to_obs_names(cls, obs_name):
        """
        Add an observatory name to the list of allowed observatory names.
        """
        if obs_name.upper() not in cls.allowed_obs_names:
            cls.allowed_obs_names.append(obs_name.upper())

    def __init__(self, obs_name):
        super().__init__()

        self.obs_name = self.add_par(
            "obs_name", obs_name.upper(), str, "Name of the observatory."
        )
        self._cfg_sub_key = self.obs_name

        if obs_name.upper() not in self.__class__.allowed_obs_names:
            self.__class__.allowed_obs_names.append(obs_name.upper())

        self.reducer = self.add_par(
            "reducer", {}, dict, "Argumnets to pass to reduction method"
        )

        self.credentials = self.add_par(
            "credentials",
            {},
            dict,
            "Credentials for the observatory or instructions for "
            "how and where to load them from. ",
        )

        self.observation_time = self.add_par(
            "observation_time",
            None,
            (None, str, int, float),
            "Time of observation. Used to propagate catalog coordinates"
            "to the time this observatory took the data using the source"
            "proper motion. Should be given as julian year format, e.g., 2018.3",
        )

        self.dataset_attribute = self.add_par(
            "dataset_attribute",
            "source_name",
            str,
            "Attribute to use for to identify a dataset",
        )
        self.dataset_identifier = self.add_par(
            "dataset_identifier",
            "key",
            str,
            "Identifier to use for to identify a dataset",
        )
        self.catalog_matching = self.add_par(
            "catalog_matching",
            "name",
            str,
            "What column in the catalog is used to match sources to datasets",
        )
        self.overwrite_files = self.add_par(
            "overwrite_files", True, bool, "Overwrite existing files"
        )
        self.save_ra_minutes = self.add_par(
            "save_ra_minutes",
            False,
            bool,
            "Save RA in minutes in addition to hours",
        )
        self.save_ra_seconds = self.add_par(
            "save_ra_seconds",
            False,
            bool,
            "Save RA in seconds in addition to hours and minutes",
        )
        self.filekey_prefix = self.add_par(
            "filekey_prefix",
            "",
            str,
            "Prefix to add to automatically generated filekeys",
        )
        self.filekey_suffix = self.add_par(
            "filekey_suffix",
            "",
            str,
            "Suffix to add to automatically generated filekeys",
        )
        self.download_batch_size = self.add_par(
            "download_batch_size",
            100,
            int,
            "Number of sources to download and hold in RAM at one time",
        )
        self.num_threads_download = self.add_par(
            "num_threads_download", 0, int, "Number of threads to use for downloading"
        )
        self.download_pars_list = self.add_par(
            "download_pars_list",
            [],
            list,
            "List of parameters that have an effect on downloading data",
        )
        self.check_download_pars = self.add_par(
            "check_download_pars",
            False,  # this check could be expensive for large data folders
            bool,
            "Check if the download parameters have changed since the last download"
            "and if so, re-download the data",
        )

        self._default_cfg_key = "observatories"
        self._enforce_no_new_attrs = False  # allow subclasses to expand attributes

        self.filtmap = self.add_par(
            "filtmap",
            None,
            (None, str, dict),
            "Mapping between observatory filter names and standard filter names",
        )

        # subclasses need to add these lines:
        # _enforce_no_new_attrs = True  # lock adding wrong attributes
        # config = load_then_update()  # load config file and update parameters
        # _apply_specific_pars(config)  # apply specific parameters for this observatory

    def _apply_specific_pars(self, inputs):
        """
        Check if parameters were given for a
        specific observatory. For example if
        one of the input keywords is "ZTF",
        that dictionary will be applied after
        the generic observatories parameters
        are ingested.

        Parameters
        ----------
        inputs : dict
            Dictionary of input parameters, usually given
            by the kwargs that are passed when constructing
            the Parameters object.
        """
        for key, value in inputs.items():
            if key.upper() == self.obs_name:
                for k, v in value.items():
                    self[k] = v

    def __setattr__(self, key, value):
        """
        Additional input validation is done using specific
        cases of __setattr__, in this case making sure
        the demo_url is a valid URL.
        """
        # ignore any keywords that match an observatory name
        if key.upper() in self.__class__.allowed_obs_names:
            return
        if key == "data_types":
            value = normalize_data_types(value)

        super().__setattr__(key, value)

    @classmethod
    def _get_default_cfg_key(cls):
        """
        Get the default key to use when loading a config file.
        """
        return "observatories"


class VirtualObservatory:
    """
    Download data from specific observatories (using subclasses).

    Base class for other virtual observatories.
    This class allows the user to load a catalog,
    and for each object in it, download data from
    some real observatory, run analysis on it,
    save the results for later, and so on.
    """

    def __init__(self, name=None):
        """
        Create a new VirtualObservatory object,
        which is a base class for other observatories,
        not to be initialized directly.

        Parameters
        ----------
        name: str
            Name of the observatory.
        """

        if not hasattr(self, "pars"):
            self.pars = None
        self.name = name
        self.cfg_hash = None  # hash of the config file (for version control)
        self._credentials = {}  # dictionary with usernames/passwords
        self._catalog = None

        # freshly downloaded data:
        self.sources = []
        self.datasets = []

        if not isinstance(self.project, str):
            raise TypeError("project name not set")

        if not isinstance(self.name, str):
            raise TypeError("Observatory name was not set")

        if "credentials" in self.pars:
            # if credentials contains a filename and key:
            self._load_passwords(**self.pars.credentials)

            # if credentials (username, password, etc.)
            # are given by the config/user inputs
            # prefer to use that instead of the file content
            self._credentials.update(self.pars.credentials)

    @property
    def project(self):
        if self.pars is None:
            return None
        return getattr(self.pars, "project", None)

    @property
    def catalog(self):
        """
        Return the catalog object.
        """
        return self._catalog

    @catalog.setter
    def catalog(self, catalog):
        """
        Set the catalog object.
        """
        if catalog == self._catalog:
            return

        if not isinstance(catalog, Catalog):
            raise TypeError("catalog must be a Catalog object")

        self._catalog = catalog

    def _load_passwords(self, filename=None, key=None, **_):
        """
        Load a YAML file with usernames, passwords, etc.

        Parameters
        ----------
        filename: str
            Name of the file to load.
            Defaults to "credentials.yaml"
        key: str
            Key inside file which is relevant to
            this observatory.
            Defaults to the observatory name
            (turned to lower case).
        """
        if filename is None:
            filename = "credentials.yaml"

        if key is None:
            key = self.name.lower()

        if os.path.isabs(filename):
            filepath = filename
        else:
            basepath = os.path.dirname(__file__)
            filepath = os.path.abspath(os.path.join(basepath, "..", filename))

        # if file doesn't exist, just return with an empty dict
        if os.path.exists(filepath):
            with open(filepath) as file:
                self._credentials = yaml.safe_load(file).get(key, {})

    def run_analysis(self):
        """
        Perform analysis on each object in the catalog.

        """
        self.analysis.load_simulator()
        self.reset_histograms()

        for row in self.catalog:
            source = self.get_source(row)
            if source is not None:
                for d in source.datasets:
                    d.load()
                self.analysis.run(source, histograms=self.histograms)

    def get_source(self, row):
        """
        Load a Source object from the database
        based on a row in the catalog.

        Parameters
        ----------
        row: dict
            A row in the catalog.
            Must contain at least an object ID
            or RA/Dec coordinates.
        """
        return None  # TODO: implement this

    def download_all_sources(
        self, start=0, stop=None, save=True, fetch_args={}, dataset_args={}
    ):
        """
        Download data from the observatory.
        Will go over all sources in the catalog,
        (unless start/stop are given),
        and download the data for each source.

        Each source data is placed in a RawPhotometry object
        (if downloading images it will be in RawImages, etc).
        If a source does not exist in the database,
        it will be created. If it already exists,
        the data will be added to the existing source.
        If save=False is given, the data is not saved to disk
        and the Source and raw data objects are not persisted to database.

        The observatory's pars.download_batch_size parameter controls
        how many sources are stored in memory at a time.
        This is useful for large catalogs, where the data
        for all sources exceeds the available RAM.

        Parameters
        ----------
        start: int, optional
            Catalog index of first source to download.
            Default is 0.
        stop: int
            Catalog index of last source to download.
            Default is None, which means the last source.
        save: bool, optional
            If True, save the data to disk and the raw data
            objects to the database (default).
            If False, do not save the data to disk or the
            raw data objects to the database (debugging only).
        fetch_args: dict, optional
            Additional arguments to pass to the
            fetch_data_from_observatory method.
        dataset_args: dict
            Additional keyword arguments to pass to the
            constructor of raw data objects.

        Returns
        -------
        num_loaded: int
            Number of sources that have been loaded
            either from memory or by downloading them.
            This is useful in case an external loop
            repeatedly calls this in batches (e.g.,
            to run analysis as in the Project class)
            and wants to know when to stop.

        """
        cat_length = len(self.catalog)
        start = 0 if start is None else start
        stop = cat_length if stop is None else stop

        self.sources = []
        self.datasets = []
        num_loaded = 0

        download_batch = max(self.pars.num_threads_download, 1)

        for i in range(start, stop, download_batch):

            if i >= cat_length:
                break

            # if temporary sources/datasets are full,
            # clear the lists before adding more
            if len(self.sources) > self.pars.download_batch_size:
                self.sources = []
                self.datasets = []

            num_threads = min(self.pars.num_threads_download, stop - i)

            if num_threads > 1:
                sources = self._fetch_data_asynchronous(
                    i, i + num_threads, save, fetch_args, dataset_args
                )
            else:  # single threaded execution
                cat_row = self.catalog.get_row(i, "number", "dict")
                s = self.check_and_fetch_source(cat_row, save, fetch_args, dataset_args)
                sources = [s]

            raw_data = []
            for s in sources:
                for dt in self.pars.data_types:
                    obs_data = None
                    for data in getattr(s, f"raw_{dt}"):
                        if data.observatory == self.name:
                            obs_data = data
                    if obs_data is not None:
                        raw_data.append(obs_data)
                    else:
                        raise RuntimeError(
                            "Cannot find data from observatory "
                            f"{self.name} on source {s.name}"
                        )

            # keep a subset of sources/datasets in memory
            self.sources += sources
            self.datasets += raw_data
            num_loaded += len(sources)

        return num_loaded

    def _fetch_data_asynchronous(self, start, stop, save, fetch_args, dataset_args):
        """
        Get data for a few sources, either by loading them
        from disk or by fetching the data online from
        the observatory.

        Each source will be handled by a separate thread.
        The following actions occur in each thread:
        (1) check if source and raw data exist in DB
        (2) if not, send a request to the observatory
        (3) if save=True, save the data to disk and the
            raw data and Source objects to the database.

        Since all these actions are I/O bound,
        it makes sense to bundle them up into threads.

        Parameters
        ----------
        start: int
            Catalog index of first source to download.
        stop: int
            Catalog index of last source to download.
        save: bool
            If True, save the data to disk and the
            raw data / Source objects to database.
        fetch_args: dict
            Additional keyword arguments to pass to the
            fetch_data_from_observatory method.
        dataset_args: dict
            Additional keyword arguments to pass to the
            constructor of raw data objects.

        Returns
        -------
        sources: list
            List of Source objects.
        """

        num_threads = stop - start

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = []
            obstime = self.pars.observation_time
            for i in range(start, stop):
                cat_row = self.catalog.get_row(
                    loc=i, index_type="number", output="dict", obstime=obstime
                )
                futures.append(
                    executor.submit(
                        self.check_and_fetch_source,
                        cat_row,
                        save,
                        fetch_args,
                        dataset_args,
                    )
                )

            done, _ = concurrent.futures.wait(
                futures, return_when=concurrent.futures.ALL_COMPLETED
            )

        sources = []
        for future in done:
            source = future.result()
            if isinstance(source, Exception):
                raise source
            if not isinstance(source, Source):
                raise RuntimeError(
                    f"Source is not a Source object, but a {type(source)}. "
                )
            sources.append(source)

        return sources

    def check_and_fetch_source(
        self, cat_row, save=True, fetch_args={}, dataset_args={}
    ):
        """
        Check if a source exists in the database,
        and if not, fetch the data from the observatory.

        Parameters
        ----------
        cat_row: dict
            A row in the catalog.
            Must contain at least an object ID
            or RA/Dec coordinates.
        save: bool
            If True, save the data to disk and the
            raw data / Source objects to database.
        fetch_args: dict
            Additional keyword arguments to pass to the
            fetch_data_from_observatory method.
        dataset_args: dict
            Additional keyword arguments to pass to the
            constructor of raw data objects.

        Returns
        -------
        source: Source
            A Source object. It should have at least
            one raw data object attached, from this
            observatory for each data type required by pars.
            (it may have more data from other observatories).

        """

        download_pars = {k: self.pars[k] for k in self.pars.download_pars_list}
        download_pars.update(
            {k: fetch_args[k] for k in self.pars.download_pars_list if k in fetch_args}
        )

        with Session() as session:
            source = session.scalars(
                sa.select(Source).where(
                    Source.name == cat_row["name"]
                )  # TODO: add cfg_hash
            ).first()
            if source is None:
                source = Source(**cat_row, project=self.project)  # TODO: add cfg_hash
                source.cat_row = cat_row  # save the raw catalog row as well

            new_data = []
            for dt in self.pars.data_types:
                data_class = get_class_from_data_type(dt)
                # if source existed in DB it should have raw data objects
                # if it doesn't that means the data needs to be downloaded
                raw_data = source.get_raw_data(
                    obs=self.name, data_type=dt, session=session
                )

                if raw_data is not None and not raw_data.check_file_exists():
                    # session.delete(raw_data)
                    # raw_data = None
                    raise RuntimeError(
                        f"{data_class} object for source {source.name} "
                        "exists in DB but file does not exist."
                    )

                # file exists, try to load it:
                if raw_data is not None:
                    lock.acquire()
                    try:
                        raw_data.load()
                    except KeyError as e:
                        if "No object named" in str(e):
                            # This does not exist in the file

                            # TODO: is delete the right thing to do?
                            source.remove_raw_data(
                                obs=self.name, data_type=dt, session=session
                            )
                            session.flush()
                            raw_data = None
                        else:
                            raise e
                    finally:
                        lock.release()

                if raw_data is not None and self.pars.check_download_pars:
                    # check if the download parameters used to save
                    # the data are consistent with those used now
                    if "download_pars" not in raw_data.altdata:
                        # TODO: is delete the right thing to do?
                        source.remove_raw_data(
                            obs=self.name, data_type=dt, session=session
                        )
                        session.flush()
                        raw_data = None
                    else:
                        for key in self.pars.download_pars_list:
                            if (
                                key not in raw_data.altdata["download_pars"]
                                or raw_data.altdata["download_pars"][key]
                                != download_pars[key]
                            ):
                                # TODO: is delete the right thing to do?
                                source.remove_raw_data(
                                    obs=self.name, data_type=dt, session=session
                                )
                                session.flush()
                                raw_data = None
                                break

                # no data on DB/file, must re-fetch from observatory website:
                if raw_data is None:
                    # <-- magic happens here! -- >
                    data, altdata = self.fetch_data_from_observatory(
                        cat_row, **fetch_args
                    )

                    # save the catalog info
                    # TODO: should we get the full catalog row?
                    altdata["cat_row"] = cat_row

                    # save the parameters involved with the download
                    altdata["download_pars"] = download_pars

                    raw_data = data_class(
                        data=data,
                        altdata=altdata,
                        observatory=self.name,
                        source_name=cat_row["name"],
                        **dataset_args,
                    )
                    # keep track of new dataset that need
                    # to be saved to disk and DB
                    new_data.append(raw_data)

                # this dataset is not appended to source yet:
                if not any(
                    [r.observatory == self.name for r in getattr(source, f"raw_{dt}")]
                ):
                    getattr(source, f"raw_{dt}").append(raw_data)

                # here we explicitly set all relational collections
                # to an empty list, so they are accessible (and empty)
                # even if the source is no longer attached to the DB.
                for name in ["reduced", "processed", "simulated"]:
                    if len(getattr(source, f"{name}_{dt}")) == 0:
                        setattr(source, f"{name}_{dt}", [])
                if len(source.detections) == 0:
                    source.detections = []
                # add more collections here...

            # unless debugging, you'd want to save this data
            if save:
                if source.ra is not None:
                    ra = source.ra
                    ra_deg = np.floor(ra)
                    if self.pars.save_ra_minutes:
                        ra_minute = np.floor((ra - ra_deg) * 60)
                    else:
                        ra_minute = None
                    if self.pars.save_ra_seconds:
                        ra_second = np.floor((ra - ra_deg - ra_minute / 60) * 3600)
                    else:
                        ra_second = None
                else:
                    ra_deg = None
                    ra_minute = None
                    ra_second = None

                try:
                    session.add(source)
                    # try to save the data to disk
                    lock.acquire()  # thread blocks at this line until it can obtain lock
                    try:
                        for data in new_data:
                            data.save(
                                overwrite=self.pars.overwrite_files,
                                source_name=source.name,
                                ra_deg=ra_deg,
                                ra_minute=ra_minute,
                                ra_second=ra_second,
                                key_prefix=self.pars.filekey_prefix,
                                key_suffix=self.pars.filekey_suffix,
                            )
                    finally:
                        lock.release()
                    # try to save the source+data to the database
                    session.commit()
                except Exception:
                    session.rollback()
                    # if saving to disk or database fails,
                    # make sure we do not leave orphans
                    for data in new_data:
                        data.delete_data_from_disk()
                    raise

        return source

    def fetch_data_from_observatory(self, cat_row, **kwargs):
        """
        Fetch data from the observatory for a given source.
        Must return a dataframe (or equivalent), even if it is an empty one.
        This must be implemented by each observatory subclass.

        Parameters
        ----------
        cat_row: dict like
            A row in the catalog for a specific source.
            In general, this row should contain the following keys:
            name, ra, dec, mag, filter_name (saying which band the mag is in).
        kwargs: dict
            Additional keyword arguments to pass to the fetcher.

        Returns
        -------
        data : pandas.DataFrame or other data structure
            Raw data from the observatory, to be put into a raw data object.
        altdata: dict
            Additional data to be stored in the raw data object.

        """
        raise NotImplementedError(
            "fetch_data_from_observatory() must be implemented in subclass"
        )

    # TODO: this should be replaced by populate raw data?
    def populate_sources(self, files_glob="*.h5", num_files=None, num_sources=None):
        """
        Read the list of files with data,
        and match them up with the catalog,
        so that each catalog row that has
        data associated with it is also
        instantiated in the database.

        Parameters
        ----------
        files_glob: str
            A glob pattern to match files with data.
            Default is '*.h5'.
        num_files: int
            The maximum number of files to read.
            If zero or None (default), all files
            found in the directory.
        num_sources: int
            The maximum number of sources to read from
            each file. Zero or None (default) means
            all sources found in each file.

        """
        if self.pars.catalog_matching == "number":
            column = "cat_index"
        elif self.pars.catalog_matching == "name":
            column = "cat_id"
        else:
            raise ValueError("catalog_matching must be either 'number' or 'name'")

        # get a list of existing sources and their ID
        source_ids = get_source_identifiers(self.project, column)

        dir = self.pars.get_data_path()
        if self.pars.verbose:
            print(f"Reading from data folder: {dir}")

        with Session() as session:
            for i, filename in enumerate(glob.glob(os.path.join(dir, files_glob))):
                if num_files and i >= num_files:
                    break

                if self.pars.verbose:
                    print(f"Reading filename: {filename}")
                # TODO: add if-else for different file types
                with pd.HDFStore(filename) as store:
                    keys = store.keys()
                    for j, k in enumerate(keys):
                        if num_sources and j >= num_sources:
                            break
                        data = store[k]
                        cat_id = self._find_dataset_identifier(data, k)
                        data_type = (
                            "photometry"  # TODO: what about multiple data types??
                        )
                        # TODO: maybe just infer the data type from the filename?
                        self.commit_source(
                            data, data_type, cat_id, source_ids, filename, k, session
                        )

        if self.pars.verbose:
            print("Done populating sources.")

    def _find_dataset_identifier(self, data, key):
        """
        Find the identifier that connects the data
        loaded from file with the catalog row and
        eventually to the source in the database.

        Parameters
        ----------
        data:
            Data loaded from file.
            Can be a dataframe or other data types.
        key:
            Key of the data loaded from file.
            For HDF5 files, this would be
            the name of the group.

        Returns
        -------
        str or int
            The identifier that connects the data
            to the catalog row.
            If self.pars.catalog_matching is 'name',
            this would be a string (the name of the source).
            If it is 'number', this would be an integer
            (the index of the source in the catalog).
        """
        if self.pars.dataset_identifier == "attribute":
            if "dataset_attribute" not in self.pars:
                raise ValueError(
                    "When using dataset_identifier='attribute', "
                    "you must specify the dataset_attribute, "
                    "that is the name of the attribute "
                    "that contains the identifier."
                )
            value = getattr(data, self.pars.dataset_attribute)
        elif self.pars.dataset_identifier == "key":
            if self.pars.catalog_matching == "number":
                value = int(re.search(r"\d+", key).group())
            elif self.pars.catalog_matching == "name":
                value = key
        else:
            raise ValueError('dataset_identifier must be "attribute" or "key"')

        if self.pars.catalog_matching == "number":
            value = int(value)

        return value

    def commit_source(
        self, data, data_type, cat_id, source_ids, filename, key, session
    ):
        """
        Save a source to the database,
        using the dataset loaded from file,
        and matching it to the catalog.
        If the source already exists in the database, nothing happens.
        If the source does not exist in the database,
        it is created, and its id is added to the source_ids.

        Parameters
        ----------
        data: dataframe or other data types
            Data loaded from file.
            For HDF5 files this is a dataframe.
        data_type: str
            The type of data loaded from file,
            e.g., 'photometry', 'spectroscopy', etc.
        cat_id: str or int
            The identifier that connects the data
            to the catalog row. Can be a string
            (the name of the source) or an integer
            (the index of the source in the catalog).
        source_ids: set of str or int
            Set of identifiers for sources that already
            exist in the database.
            Any new data with the same identifier is skipped,
            and any new data not in the set is added.
        filename: str
            Full path to the file from which the data was loaded.
        key: str
            Key inside the file if multiple datasets are in each file.
        session: sqlalchemy.orm.session.Session
            The current session to which we add
            newly created sources.

        """
        if self.pars.verbose > 1:
            print(
                f"Loaded data for source {cat_id} | "
                f"len(data): {len(data)} | "
                f"id in source_ids: {cat_id in source_ids}"
            )

        if len(data) <= 0:
            return  # no data

        if cat_id in source_ids:
            return  # source already exists

        row = self.catalog.get_row(cat_id, self.pars.catalog_matching)

        (
            index,
            name,
            ra,
            dec,
            mag,
            mag_err,
            filter_name,
            alias,
        ) = self.catalog.values_from_row(row)

        new_source = Source(
            name=name,
            ra=ra,
            dec=dec,
            project=self.project,
            alias=[alias] if alias else None,
            mag=mag,
            mag_err=mag_err,
            mag_filter=filter_name,
        )
        new_source.cat_id = name
        new_source.cat_index = index
        new_source.cat_name = self.catalog.pars.catalog_name

        data_class = get_class_from_data_type(data_type)
        raw_data = data_class(
            data=data,
            observatory=self.name,
            filename=filename,
            key=key,
        )

        setattr(new_source, f"raw_{data_type}", [raw_data])
        setattr(new_source, f"reduced_{data_type}", self.reduce(new_source))
        for d in getattr(new_source, f"reduced_{data_type}"):
            d.save()

        session.add(new_source)
        session.commit()
        source_ids.add(cat_id)

        # TODO: is here the point where we also do analysis?

    def reduce(self, source, data_type=None, output_type=None, **kwargs):
        """
        Reduce raw data into more useful,
        second level (reduced) data products.
        Input raw data could be
        raw photometry, images, cutouts, spectra.
        The type of reduction to use is inferred
        from the dataset's "type" attribute.

        The output that should be produced from
        the raw data can be lightcurves (i.e.,
        processed photometry ready for analysis)
        or SED (i.e., Spectral Energy Distribution,
        which is just a reduced spectra ready for analysis)
        or even just calibrating an image.

        Parameters
        ----------
        source: src.source.Source object
            A source with datasets to be reduced.
            Can also give a raw data object
            (e.g., a RawPhotometry object) instead.
            If so, the data_type will be inferred from the object.
            In this case no source is given to the reduction function.
        data_type: str (optional)
            The type of data to reduce.
            Can be one of 'photometry', 'spectroscopy', 'images', 'cutouts'.
            If None, will try to infer the type from the data.
        output_type: str (optional)
            The type of output to produce.
            Possible values are:
            "lcs", "sed", "img", "thumb".
            If the input type is photometric data,
            the output_type will be replaced by "lcs".
            If the input type is a spectrum,
            the output_type will be replaced by "sed".
            Imaging data can be reduced into
            "img" (a calibrated image), "thumb" (a thumbnail),
            or "lcs" (a lightcurve of extracted sources).
            By default, this is inferred by the input data type.
        kwargs: dict
            Additional arguments to pass to the reduction function.

        Returns
        -------
        an object of a subclass of src.dataset.Dataset
            The reduced dataset,
            can be, e.g., a Lightcurve object.
        """

        # this is called without a session,
        # so raw data must be attached to source
        if isinstance(source, Source):
            dataset = source.get_raw_data(obs=self.name, data_type=data_type)
            data_type = convert_data_type(data_type)
        elif isinstance(source, DatasetMixin):
            dataset = source
            data_type = dataset.type
            source = None
        else:
            raise TypeError("source must be a Source or a dataset object")

        # parameters for the reduction
        # are taken from the config first,
        # then from the user inputs
        if "reducer" in self.pars and isinstance(self.pars.reducer, dict):
            parameters = {}
            parameters.update(self.pars.reducer)
            parameters.update(kwargs)
            kwargs = parameters

        # arguments to be passed into the new dataset constructors
        init_kwargs = self._make_init_kwargs(dataset)

        # choose which kind of reduction to do
        if output_type is None:
            output_type = data_type
        else:
            output_type = convert_data_type(output_type)

        # get the name of the reducer function
        if data_type == output_type:  # same type reductions
            reducer_name = f"reduce_{data_type}"
        else:  # cross reductions like image->lightcurve
            reducer_name = f"reduce_{data_type}_to_{output_type}"

        # get the reducer function
        if hasattr(self, reducer_name):
            reducer = getattr(self, reducer_name)
        else:
            reducer = None

        # check the reducer function is legit
        if reducer is None or not callable(reducer):
            raise NotImplementedError(f'No reduction method "{reducer_name}" found.')

        # TODO: should we allow using source=None?
        new_datasets = reducer(dataset, source, init_kwargs, **kwargs)
        new_datasets = sorted(new_datasets, key=lambda x: x.time_start)

        # copy some properties of the observatory into the new datasets
        copy_attrs = ["project", "cfg_hash"]
        for d in new_datasets:
            for attr in copy_attrs:
                setattr(d, attr, getattr(self, attr))

        # make sure each reduced dataset is associated with a source
        for d in new_datasets:
            d.source = source

        if source is not None:
            old_datasets = getattr(source, f"reduced_{output_type}")
            setattr(source, f"reduced_{output_type}", old_datasets + new_datasets)

        # make sure each reduced dataset has a serial number:
        for i, d in enumerate(new_datasets):
            d.reduction_number = i + 1
            d.reduction_total = len(new_datasets)

        return new_datasets

    def _make_init_kwargs(self, dataset):
        """
        Make a dictionary of arguments to pass to the
        constructor of a new (reduced) dataset.

        Parameters
        ----------
        dataset: a raw data object like src.dataset.RawPhotometry
            The raw data to be reduced. Some info from this
            data will be copied into the new dataset.

        Returns
        -------
        dict:
            A dictionary of arguments to pass to the constructor
            of a new dataset.
        """
        # arguments to be passed into the new dataset constructors
        init_kwargs = {}
        for att in DatasetMixin.default_copy_attributes:
            if hasattr(dataset, att):
                init_kwargs[att] = getattr(dataset, att)

        # TODO: What if dataset has not been committed yet and has no id?
        init_kwargs["raw_data_id"] = dataset.id
        init_kwargs["raw_data"] = dataset

        # TODO: what if dataset has not been saved yet and has no filename?
        if "raw_data_filename" not in init_kwargs:
            init_kwargs["raw_data_filename"] = dataset.filename

        for att in DatasetMixin.default_update_attributes:
            new_dict = {}
            new_value = getattr(dataset, att)
            if isinstance(new_value, dict):
                new_dict.update(new_value)
            if len(new_dict) > 0:
                init_kwargs[att] = new_dict

        init_kwargs["filtmap"] = self.pars.filtmap

        return init_kwargs

    def help(self=None, owner_pars=None):
        """
        Print the help for this object and objects contained in it.
        """
        if isinstance(self, VirtualObservatory):
            help_with_object(self, owner_pars)
        elif self is None or self == VirtualObservatory:
            help_with_class(VirtualObservatory, ParsObservatory)


class ParsDemoObs(ParsObservatory):

    # must register this observatory in list of allowed names
    ParsObservatory.add_to_obs_names("DEMO")

    def __init__(self, **kwargs):

        super().__init__("demo")

        self.demo_boolean = self.add_par(
            "demo_boolean", True, bool, "A boolean parameter"
        )
        self.demo_string = self.add_par("demo_string", "foo", str, "A string parameter")
        self.demo_url = self.add_par(
            "demo_url", "http://www.example.com", str, "A URL parameter"
        )

        self.wait_time = self.add_par(
            "wait_time", 0.0, float, "Time to wait to simulate downloading from web."
        )

        self.wait_time_poisson = self.add_par(
            "wait_time_poisson",
            0.0,
            float,
            "Time to wait to simulate downloading from web, "
            "with a Poisson distribution random number.",
        )

        self.sim_args = self.add_par(
            "sim_args", {}, dict, "Arguments to pass to the simulator."
        )

        self.download_pars_list = ["wait_time", "wait_time_poisson", "sim_args"]

        self._enforce_no_new_attrs = True

        config = self.load_then_update(kwargs)

        # apply parameters specific to this class
        self._apply_specific_pars(config)

    def __setattr__(self, key, value):
        """
        Additional input validation is done using specific
        cases of __setattr__, in this case making sure
        the demo_url is a valid URL.
        """
        if key == "demo_url":
            validators.url(value)

        super().__setattr__(key, value)


class VirtualDemoObs(VirtualObservatory):
    """
    A demo observatory that produces simulated data.

    This is useful for testing and demonstration purposes.
    To get actual data from real observations, use the
    real VirtualObservatory sub classes, e.g., VirtualZTF.
    """

    def __init__(self, **kwargs):
        """
        Generate an instance of a VirtualDemoObs object.
        This object can be used to test basic operations
        of this package, like pretending to download data,
        running analysis, etc.

        Parameters
        ----------
        Are the same as the VirtualObservatory class.
        The only difference is that the obs_name is set to "demo".

        """
        # TODO: separate reducer into its own object
        # reducer_kwargs = kwargs.pop("reducer_kwargs", {})

        self.pars = ParsDemoObs(**kwargs)
        # call this only after a pars object is set up
        super().__init__(name="demo")

    def fetch_data_from_observatory(
        self,
        cat_row,
        wait_time=None,
        wait_time_poisson=None,
        verbose=False,
        sim_args={},
    ):
        """
        Fetch data from the observatory for a given source.
        Since this is a demo observatory it would not actually
        fetch anything. Instead, it will generate random data
        and return it after a short pause.

        Parameters
        ----------
        cat_row: dict like
            A row in the catalog for a specific source.
            In general, this row should contain the following keys:
            name, ra, dec, mag, filter_name (saying which band the mag is in).
        wait_time: int or float, optional
            If given, will make the simulator pause for a short time.
            Can either use the value in the parameters object or pass it
            in directly, which will override the parameter.
        wait_time_poisson: bool, optional
            Will add a randomly selected integer number of seconds
            (from a Poisson distribution) to the wait time.
            The mean of the distribution is the value given
            to wait_time_poisson.
            Can either use the value in the parameters object or pass it
            in directly, which will override the parameter.
        verbose: bool, optional
            If True, will print out some information about the
            data that is being fetched or simulated.
        sim_args: dict
            A dictionary passed into the simulate_lightcuve function.
            Can either use the value in the parameters object or pass it
            in directly, which will override the parameter.

        Returns
        -------
        data : pandas.DataFrame or other data structure
            Raw data from the observatory, to be put into a RawPhotometry object.
        altdata: dict
            Additional data to be stored in the RawPhotometry object.

        """
        if wait_time is None:
            wait_time = self.pars.wait_time
        if wait_time_poisson is None:
            wait_time_poisson = self.pars.wait_time_poisson
        if sim_args is not None:
            # need a copy, so we don't change the original dict in pars:
            sim_args_default = copy.deepcopy(self.pars.sim_args)
            sim_args_default.update(sim_args)
            sim_args = sim_args_default

        if verbose:
            print(
                f'Fetching data from demo observatory for source {cat_row["cat_index"]}'
            )
        total_wait_time_seconds = wait_time + np.random.poisson(wait_time_poisson)
        data = self.simulate_lightcurve(**sim_args)
        altdata = {
            "demo_boolean": self.pars.demo_boolean,
            "wait_time": total_wait_time_seconds,
        }

        time.sleep(total_wait_time_seconds)

        if verbose:
            print(
                f'Finished fetch data for source {cat_row["cat_index"]} '
                f"after {total_wait_time_seconds} seconds"
            )

        return data, altdata

    @staticmethod
    def simulate_lightcurve(
        num_points=100,
        mjd_range=(57000, 58000),
        shuffle_time=False,
        mag_err_range=(0.09, 0.11),
        mean_mag=18,
        exptime=30,
        filter="R",
    ):
        if shuffle_time:
            mjd = np.random.uniform(mjd_range[0], mjd_range[1], num_points)
        else:
            mjd = np.linspace(mjd_range[0], mjd_range[1], num_points)

        mag_err = np.random.uniform(mag_err_range[0], mag_err_range[1], num_points)
        mag = np.random.normal(mean_mag, mag_err, num_points)
        flag = np.zeros(num_points, dtype=bool)
        test_data = dict(mjd=mjd, mag=mag, mag_err=mag_err, filter=filter, flag=flag)
        df = pd.DataFrame(test_data)
        df["exptime"] = exptime

        return df

    def reduce_photometry(
        self, dataset, source=None, init_kwargs={}, mag_range=None, drop_bad=False, **_
    ):
        """
        Reduce the dataset.

        Parameters
        ----------
        dataset: a src.dataset.RawPhotometry object or other data types
            The raw data to reduce. Can be photometry, images, etc.
        source: src.source.Source object
            The source to which the dataset belongs.
            If None, the reduction will not use any
            data of the source, such as the expected
            magnitude, the position, etc.
        init_kwargs: dict
            A dictionary of keyword arguments to be
            passed to the constructor of the new dataset.
        mag_range: float or None
            If not None, and if the source is also given,
            this value will be used to remove datasets
            where the median magnitude is outside of this range,
            relative to the source's magnitude.
        drop_bad: bool
            If True, any points in the lightcurves will be
            dropped if their flag is non-zero
            or if their magnitude is NaN.
            This reduces the output size but will
            also not let bad data be transferred
            down the pipeline for further review.

        Returns
        -------
        a list of src.dataset.Lightcurve objects
            The reduced datasets, after minimal processing.
            The reduced datasets will have uniform filter,
            each dataset will be sorted by time,
            and some initial processing will be done,
            using the "reducer" parameter (or function inputs).
        """
        allowed_dataclasses = pd.DataFrame
        if not isinstance(dataset, RawPhotometry):
            raise TypeError(
                f"dataset must be a RawPhotometry object, not {type(dataset)}"
            )
        if not isinstance(dataset.data, allowed_dataclasses):
            raise ValueError(
                f"Expected RawPhotometry to contain {str(allowed_dataclasses)}, "
                f"but data was given as a {type(dataset.data)} object."
            )

        # check the source magnitude is within the range
        if source and source.mag is not None and mag_range:
            mag = dataset.data[dataset.mag_col]
            mag_mx = source.mag + mag_range
            mag_mn = source.mag - mag_range
            if not mag_mn < np.nanmedian(mag) < mag_mx:
                return []  # this dataset is not within the range

        # split the data by filters
        if isinstance(dataset.data, pd.DataFrame):
            # make sure there is some photometric data available
            filt_col = dataset.colmap["filter"]
            flag_col = dataset.colmap["flag"] if "flag" in dataset.colmap else None
            filters = dataset.data[filt_col].unique()
            dfs = []
            for f in filters:
                # new dataframe for each filter, each one with a new index
                df_new = dataset.data.loc[dataset.data[filt_col] == f, :].copy()
                df_new.reset_index(drop=True)

                if drop_bad and flag_col is not None:
                    df_new = df_new[df_new[flag_col] == 0]

                dfs.append(df_new)
                # TODO: what happens if filter is in altdata, not in dataframe?

            new_datasets = []
            for df in dfs:
                new_datasets.append(Lightcurve(data=df, **init_kwargs))

        return new_datasets

    def help(self=None, owner_pars=None):
        """
        Print the help for this object and objects contained in it.
        """
        if isinstance(self, VirtualDemoObs):
            help_with_object(self, owner_pars)
        elif self is None or self == VirtualDemoObs:
            help_with_class(VirtualDemoObs, ParsDemoObs)


if __name__ == "__main__":
    from src.catalog import Catalog
    from src import dataset

    dataset.DATA_ROOT = "/home/guyn/data"

    obs = VirtualDemoObs(num_threads_download=5, project="test")
    # obs = VirtualObservatory()
    cat = Catalog(default="WD")
    cat.load()
    obs.catalog = cat
