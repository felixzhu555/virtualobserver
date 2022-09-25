import time
import os
import glob
import re
import yaml
import validators
import numpy as np
import pandas as pd
import threading
import concurrent.futures

import sqlalchemy as sa
from sqlalchemy.orm import joinedload
from src.database import Session
from src.parameters import Parameters
from src.source import Source, get_source_identifiers
from src.dataset import DatasetMixin, RawData, Lightcurve
from src.detection import DetectionInTime

from src.catalog import Catalog
from src.histogram import Histogram
from src.analysis import Analysis

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

    def __init__(self):
        super().__init__()

        self.obs_name = self.add_par("obs_name", "", str, "Name of the observatory.")
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
        self.save_ra_minutes = (
            self.add_par(
                "save_ra_minutes",
                False,
                bool,
                "Save RA in minutes in addition to hours",
            ),
        )
        self.save_ra_seconds = (
            self.add_par(
                "save_ra_seconds",
                False,
                bool,
                "Save RA in seconds in addition to hours and minutes",
            ),
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

        self._default_cfg_key = "observatories"
        self._enforce_type_checks = True
        self._enforce_no_new_attrs = False  # allow subclasses to expand attributes

        # subclass need to add these lines:
        # _enforce_no_new_attrs = True  # lock adding wrong attributes
        # config = load_then_update()  # load config file and update parameters
        # apply_specific_pars(config)  # apply specific parameters for this observatory

    def add_obs_name(self, name):
        """
        Add an observatory name to the list of allowed names.
        """
        self.obs_name = name.upper()

        if name.upper() not in self.allowed_obs_names:
            self.allowed_obs_names.append(name.upper())

    def apply_specific_pars(self, inputs):
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
        if key.upper() in self.allowed_obs_names:
            return

        super().__setattr__(key, value)


class VirtualObservatory:
    """
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
            self.load_passwords(**self.pars.credentials)

            # if credentials (username, password, etc.)
            # are given by the config/user inputs
            # prefer to use that instead of the file content
            self._credentials.update(self.pars.credentials)

    @property
    def project(self):
        if hasattr(self, "pars"):
            return self.pars.project
        else:
            return None

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

    def load_passwords(self, filename=None, key=None, **_):
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

        Each source data is placed in a RawData object.
        If a source does not exist in the database,
        it will be created. If it already exists,
        the data will be added to the existing source.
        If save=False is given, the data is not saved to disk
        and the Source and RawData objects are not persisted to database.

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
            If True, save the data to disk and the RawData
            objects to the database (default).
            If False, do not save the data to disk or the
            RawData objects to the database (debugging only).
        dataset_args: dict
            Additional keyword arguments to pass to the
            download method of the specific observatory.
        dataset_args: dict
            Additional keyword arguments to pass to the
            constructor of RawData objects.

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
                sources = self.fetch_data_asynchronous(
                    i, i + num_threads, save, fetch_args, dataset_args
                )
            else:  # single threaded execution
                cat_row = self.catalog.get_row(i, "number", "dict")
                s = self.check_and_fetch_source(cat_row, save, fetch_args, dataset_args)
                sources = [s]

            raw_data = []
            for s in sources:
                this_data = None
                for d in s.raw_data:
                    if d.observatory == self.name:
                        this_data = d
                if this_data is not None:
                    raw_data.append(this_data)
                else:
                    raise RuntimeError(
                        f"Cannot find data from this observatory on source {s.name}"
                    )

            # keep a subset of sources/datasets in memory
            self.sources += sources
            self.datasets += raw_data
            num_loaded += len(sources)

        return num_loaded

    def fetch_data_asynchronous(self, start, stop, save, fetch_args, dataset_args):
        """
        Get data for a few sources, either by loading them
        from disk or by fetching the data online from
        the observatory.

        Each source will be handled by a separate thread.
        The following actions occur in each thread:
        (1) check if source and raw data exist in DB
        (2) if not, send a request to the observatory
        (3) if save=True, save the data to disk and the
            RawData and Source objects to the database.

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
            RawData / Source objects to database.
        fetch_args: dict
            Additional keyword arguments to pass to the
            fetch_data_from_observatory method.
        dataset_args: dict
            Additional keyword arguments to pass to the
            constructor of RawData objects.

        Returns
        -------
        sources: list
            List of Source objects.
        """

        num_threads = stop - start

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = []
            for i in range(start, stop):
                cat_row = self.catalog.get_row(i, "number", "dict")
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
            RawData / Source objects to database.
        fetch_args: dict
            Additional keyword arguments to pass to the
            fetch_data_from_observatory method.
        dataset_args: dict
            Additional keyword arguments to pass to the
            constructor of RawData objects.

        Returns
        -------
        source: Source
            A Source object. It should have at least
            one RawData object attached, from this
            observatory (it may have more data from
            other observatories).

        """
        with Session() as session:
            source = session.scalars(
                sa.select(Source).where(Source.name == cat_row["name"])
                # .options(joinedload(Source.raw_data))
            ).first()
            if source is None:
                source = Source(**cat_row, project=self.project)

            raw_data = session.scalars(
                sa.select(RawData).where(
                    RawData.source_name == source.name, RawData.observatory == self.name
                )
            ).first()
            # remove RawData objects that have no file
            if raw_data is not None and not raw_data.check_file_exists():
                session.delete(raw_data)
                raw_data = None

            # file exists, try to load it:
            if raw_data is not None:
                lock.acquire()
                try:
                    raw_data.load()
                except KeyError as e:
                    if "No object named" in str(e):
                        # This does not exist in the file
                        session.delete(raw_data)
                        raw_data = None
                    else:
                        raise e
                finally:
                    lock.release()

            # no data on DB/file, must re-fetch from observatory website:
            if raw_data is None:
                # <-- magic happens here! -- >
                data, altdata = self.fetch_data_from_observatory(cat_row, **fetch_args)
                raw_data = RawData(
                    data=data,
                    altdata=altdata,
                    observatory=self.name,
                    **dataset_args,
                )

            if not any([r.observatory == self.name for r in source.raw_data]):
                source.raw_data.append(raw_data)

            if raw_data.source is None:
                raw_data.source = source

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
                        raw_data.save(
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
                except:
                    # if saving to disk or database fails,
                    # make sure we do not leave orphans
                    session.rollback()

                    # did this RawData object already exist?
                    # if so, do not remove the file that goes with it...
                    raw_data_check = session.scalars(
                        sa.select(RawData).filter(
                            RawData.source_id == source.id,
                            RawData.observatory == self.name,
                        )
                    ).first()
                    if raw_data_check is None:
                        # the raw data is not in the database,
                        # so delete from disk the data matching this
                        # new RawData object
                        raw_data.delete_data_from_disk()
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
            Raw data from the observatory, to be put into a RawData object.
        altdata: dict
            Additional data to be stored in the RawData object.

        """
        raise NotImplementedError(
            "fetch_data_from_observatory() must be implemented in subclass"
        )

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
                        cat_id = self.find_dataset_identifier(data, k)
                        self.save_source(data, cat_id, source_ids, filename, k, session)

        if self.pars.verbose:
            print("Done populating sources.")

    def find_dataset_identifier(self, data, key):
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

    def save_source(self, data, cat_id, source_ids, filename, key, session):
        """
        Save a source to the database,
        using the dataset loaded from file,
        and matching it to the catalog.
        If the source already exists in the database,
        nothing happens.
        If the source does not exist in the database,
        it is created and it's id is added to the source_ids.

        Parameters
        ----------
        data: dataframe or other data types
            Data loaded from file.
            For HDF5 files this is a dataframe.
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

        raw_data = RawData(
            data=data,
            observatory=self.name,
            filename=filename,
            key=key,
        )

        new_source.raw_data = [raw_data]
        new_source.lightcurves = self.reduce(raw_data, to="lcs", source=new_source)
        for lc in new_source.lightcurves:
            lc.save()

        session.add(new_source)
        session.commit()
        source_ids.add(cat_id)

        # TODO: is here the point where we also do analysis?

    def reduce(self, dataset, to="lcs", source=None, **kwargs):
        """
        Reduce raw data into more useful,
        second level data product.
        Input raw data could be
        raw photometry, images, cutouts, spectra.
        The type of reduction to use is inferred
        from the dataset's "type" attribute.

        The output that should be produced from
        the raw data can be lightcurves (i.e.,
        processed photometry ready for analysis)
        or SED (i.e., Spectral Energy Distribution,
        which is just a reduced spectra ready for
        analysis) or even just calibrating an image.
        Possible values for the "to" input are:
        "lcs", "sed", "img", "thumb".

        Parameters
        ----------
        dataset: a src.dataset.RawData object (or list of such objects)
            The raw data to reduce.
        to: str
            The type of output to produce.
            Possible values are:
            "lcs", "sed", "img", "thumb".
            If the input type is photometric data,
            the "to" will be replaced by "lcs".
            If the input type is a spectrum,
            the "to" will be replaced by "sed".
            Imaging data can be reduced into
            "img" (a calibrated image), "thumb" (a thumbnail),
            or "lcs" (a lightcurve of extracted sources).
        source: src.source.Source object
            The source to which the dataset belongs.
            If None, the reduction will not use any
            data of the source, such as the expected
            magnitude, the position, etc.
        kwargs: dict
            Additional arguments to pass to the reduction function.

        Returns
        -------
        an object of a subclass of src.dataset.Dataset
            The reduced dataset,
            can be, e.g., a Lightcurve object.
        """
        if isinstance(dataset, list):
            datasets = dataset
        else:
            datasets = [dataset]

        for i, d in enumerate(datasets):
            if not isinstance(d, RawData):
                raise ValueError(
                    f"Expected RawData object, but dataset {i} was a {type(d)}"
                )

        # parameters for the reduction
        # are taken from the config first,
        # then from the user inputs
        if "reducer" in self.pars and isinstance(self.pars.reducer, dict):
            parameters = {}
            parameters.update(self.pars.reducer)
            parameters.update(kwargs)
            kwargs = parameters

        # arguments to be passed into the new dataset constructors
        init_kwargs = {}
        for att in DatasetMixin.default_copy_attributes:
            values = list({getattr(d, att) for d in datasets})
            # only copy values if they're the same
            # for all source (raw) datasets
            if len(values) == 1:
                init_kwargs[att] = values[0]

        # if all data comes from a single raw dataset
        # we should link back to it from the new datasets
        raw_data_ids = list({d.id for d in datasets})
        if len(raw_data_ids) == 1:
            init_kwargs["raw_data_id"] = raw_data_ids[0]
            init_kwargs["raw_data"] = datasets[0]
        raw_data_filenames = list({d.filename for d in datasets})
        if "raw_data_filename" not in init_kwargs and len(raw_data_filenames) == 1:
            init_kwargs["raw_data_filename"] = raw_data_filenames[0]

        # all datasets come from the same source?
        if source is None:
            source_names = list({d.source_name for d in datasets if d.source_name})
            if len(source_names) == 1:
                source = datasets[0].source
        elif source is False:  # excplicitly don't use source
            source = None

        for att in DatasetMixin.default_update_attributes:
            new_dict = {}
            for d in datasets:
                new_value = getattr(d, att)
                if isinstance(new_value, dict):
                    new_dict.update(new_value)
            if len(new_dict) > 0:
                init_kwargs[att] = new_dict

        if "filtmap" in self.pars:
            if not isinstance(self.pars.filtmap, (str, dict)):
                raise ValueError("filtmap must be a string or a dictionary")
            init_kwargs["filtmap"] = self.pars.filtmap

        # choose which kind of reduction to do
        if to.lower() in ("lc", "lcs", "lightcurves", "photometry"):
            new_datasets = self.reduce_to_lightcurves(
                datasets, source, init_kwargs, **kwargs
            )
        elif to.lower() in ("sed", "seds", "spectra", "spectrum"):
            new_datasets = self.reduce_to_sed(datasets, source, init_kwargs, **kwargs)
        elif to.lower() == "img":
            new_datasets = self.reduce_to_images(
                datasets, source, init_kwargs, **kwargs
            )
        elif to.lower() == "thumb":
            new_datasets = self.reduce_to_thumbnail(
                datasets, source, init_kwargs, **kwargs
            )
        else:
            raise ValueError(f'Unknown value for "to" input: {to}')

        new_datasets = sorted(new_datasets, key=lambda d: d.time_start)

        # copy some properties of the observatory into the new datasets
        copy_attrs = ["project", "cfg_hash"]
        for d in new_datasets:
            for attr in copy_attrs:
                setattr(d, attr, getattr(self, attr))

        # make sure each reduced dataset is associated with a source
        for d in new_datasets:
            d.source = source

        # make sure each reduced dataset has a serial number:
        for i, d in enumerate(new_datasets):
            d.reduction_number = i + 1
            d.reduction_total = len(new_datasets)

        return new_datasets

    def reduce_to_lightcurves(self, datasets, source=None, init_kwargs={}, **kwargs):
        raise NotImplementedError("Photometric reduction not implemented in this class")

    def reduce_to_sed(self, datasets, source=None, init_kwargs={}, **kwargs):
        raise NotImplementedError("SED reduction not implemented in this class")

    def reduce_to_images(self, datasets, source=None, init_kwargs={}, **kwargs):
        raise NotImplementedError("Image reduction not implemented in this class")

    def reduce_to_thumbnail(self, datasets, source=None, init_kwargs={}, **kwargs):
        raise NotImplementedError("Thumbnail reduction not implemented in this class")


class ParsDemoObs(ParsObservatory):
    def __init__(self, **kwargs):
        super().__init__()

        self.add_obs_name("demo")

        self.demo_boolean = self.add_par(
            "demo_boolean", True, bool, "A boolean parameter"
        )
        self.demo_url = self.add_par(
            "demo_url", "http://www.example.com", str, "A URL parameter"
        )

        self._enforce_type_checks = True
        self._enforce_no_new_attrs = True

        config = self.load_then_update(kwargs)

        # apply parameters specific to this class
        self.apply_specific_pars(config)

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
        self, cat_row, wait_time=0, wait_time_poisson=0, verbose=False, sim_args={}
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
        wait_time_poisson: bool, optional
            Will add a randomly selected integer number of seconds
            (from a Poisson distribution) to the wait time.
            The mean of the distribution is the value given
            to wait_time_poisson.
        verbose: bool, optional
            If True, will print out some information about the
            data that is being fetched or simulated.
        sim_args: dict
            A dictionary passed into the simulate_lightcuve function.

        Returns
        -------
        data : pandas.DataFrame or other data structure
            Raw data from the observatory, to be put into a RawData object.
        altdata: dict
            Additional data to be stored in the RawData object.

        """

        if verbose:
            print(
                f'Fetching data from demo observatory for source {cat_row["cat_index"]}'
            )
        wait_time_seconds = wait_time + np.random.poisson(wait_time_poisson)
        data = self.simulate_lightcurve(**sim_args)
        altdata = {
            "demo_boolean": self.pars.demo_boolean,
            "wait_time": wait_time_seconds,
        }

        time.sleep(wait_time_seconds)

        if verbose:
            print(
                f'Finished fetch data for source {cat_row["cat_index"]} after {wait_time_seconds} seconds'
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

    def reduce_to_lightcurves(
        self, datasets, source=None, init_kwargs={}, mag_range=None, drop_bad=False, **_
    ):
        """
        Reduce the datasets to lightcurves.

        Parameters
        ----------
        datasets: a list of src.dataset.RawData objects
            The raw data to reduce.
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
        allowed_types = "photometry"
        allowed_dataclasses = pd.DataFrame
        if isinstance(datasets, RawData):
            datasets = [datasets]
        for i, d in enumerate(datasets):
            # check the raw input types make sense
            if d.type is None or d.type not in allowed_types:
                raise ValueError(
                    f"Expected RawData to contain {str(allowed_types)}, "
                    f"but dataset {i} was a {d.type} dataset."
                )
            if not isinstance(d.data, allowed_dataclasses):
                raise ValueError(
                    f"Expected RawData to contain {str(allowed_dataclasses)}, "
                    f"but data in dataset {i} was a {type(d.data)} object."
                )

        # check the source magnitude is within the range
        if source and source.mag is not None and mag_range:
            # need to make a copy of the list so we don't
            # delete what we are iterating over!
            for d in list(datasets):
                mag = d.data[d.mag_col]

                if not (
                    source.mag - mag_range < np.nanmedian(mag) < source.mag + mag_range
                ):
                    datasets.remove(d)

        # split the data by filters
        # (assume all datasets have the same data class
        # and that the internal column structure is the same
        # which is a reasonable assumption as they all come
        # from the same observatory)
        if isinstance(datasets[0].data, pd.DataFrame):
            # make sure there is some photometric data available

            frames = [d.data for d in datasets]
            all_dfs = pd.concat(frames)

            filt_col = datasets[0].colmap["filter"]
            flag_col = (
                datasets[0].colmap["flag"] if "flag" in datasets[0].colmap else None
            )
            filters = all_dfs[filt_col].unique()
            dfs = []
            for f in filters:
                # new dataframe for each filter
                # each one with a new index
                df_new = all_dfs[all_dfs[filt_col] == f].reset_index(drop=True)
                # df_new = df_new.sort([datasets[0].time_col], inplace=False)
                # df_new.reset_index(drop=True, inplace=True)
                if drop_bad and flag_col is not None:
                    df_new = df_new[df_new[flag_col] == 0]

                dfs.append(df_new)
                # TODO: what happens if filter is in altdata, not in dataframe?

            new_datasets = []
            for df in dfs:
                new_datasets.append(Lightcurve(data=df, **init_kwargs))

        return new_datasets


if __name__ == "__main__":
    from src.catalog import Catalog
    from src import dataset

    dataset.DATA_ROOT = "/home/guyn/data"

    obs = VirtualDemoObs(num_threads_download=5)
    # obs = VirtualObservatory()
    obs.project = "WD"
    cat = Catalog(default="WD")
    cat.load()
    obs.catalog = cat
