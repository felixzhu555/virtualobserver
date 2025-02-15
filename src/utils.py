"""
Various utility functions and classes
that were not relevant to any specific module.
"""
import sys
from datetime import datetime, timezone
import dateutil.parser

from inspect import signature
from astropy.coordinates import SkyCoord
from astropy import units as u
from astropy.time import Time


class OnClose:
    """
    Create an instance of this class so that it
    runs the given function/lambda when it goes
    out of scope.
    This could be useful for removing files,
    deleting things from the DB, and so on.
    It triggers even if there is an exception,
    so it is kind of like a finally block.
    """

    def __init__(self, func):
        if not callable(func):
            raise TypeError("func must be callable")
        self.func = func

    def __del__(self):
        self.func()


def trim_docstring(docstring):
    """
    Remove leading and trailing lines, remove indentation, etc.
    See PEP 257: https://peps.python.org/pep-0257/
    """
    if not docstring:
        return ""
    # Convert tabs to spaces (following the normal Python rules)
    # and split into a list of lines:
    lines = docstring.expandtabs().splitlines()
    # Determine minimum indentation (first line doesn't count):
    indent = sys.maxsize
    for line in lines[1:]:
        stripped = line.lstrip()
        if stripped:
            indent = min(indent, len(line) - len(stripped))
    # Remove indentation (first line is special):
    trimmed = [lines[0].strip()]
    if indent < sys.maxsize:
        for line in lines[1:]:
            trimmed.append(line[indent:].rstrip())
    # Strip off trailing and leading blank lines:
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    while trimmed and not trimmed[0]:
        trimmed.pop(0)
    # Return a single string:
    return "\n".join(trimmed)


def short_docstring(docstring):
    """
    Get the first line of the docstring.
    Assumes the docstring has already been cleared
    of leading new lines and indentation.
    """
    if not docstring:
        return ""

    return docstring.splitlines()[0]


def help_with_class(cls, pars_cls=None, sub_classes=None):
    """
    Print the help for this object and objects contained in it.

    Parameters
    ----------
    cls : class
        The class to print help for.
    pars_cls : class, optional
        The class that contains the parameters for this class.
        If None, no parameters are printed.
    sub_classes : list of classes, optional
        A list of classes that are contained in this class.
        The help for each of those will be printed.
    """
    description = short_docstring(trim_docstring(cls.__doc__))

    print(f"{cls.__name__}\n" "--------\n" f"{description}")

    print_functions(cls)

    if pars_cls is not None:
        print("Parameters:")
        # initialize a parameters object and print it
        pars = pars_cls(cfg_file=False)  # do not read config file
        pars.print()  # show a list of parameters
        print()  # newline

    if sub_classes is not None:
        for sub_cls in sub_classes:
            if hasattr(sub_cls, "help") and callable(sub_cls.help):
                sub_cls.help()
        print()  # newline


def help_with_object(obj, owner_pars):
    """
    Print the help for this object and all sub-objects that know how to print help.
    """
    description = short_docstring(trim_docstring(obj.__class__.__doc__))
    this_pars = None
    print(f"{obj.__class__.__name__}*\n" "--------\n" f"{description}")

    print_functions(obj)

    if hasattr(obj, "pars"):
        print("Parameters:")
        obj.pars.print(owner_pars)
        print()  # newline
        this_pars = obj.pars

    for k, v in obj.__dict__.items():
        if not k.startswith("_"):
            if hasattr(v, "help") and callable(v.help):
                v.help(this_pars)
            elif hasattr(v, "__len__"):
                for li in v:
                    if hasattr(li, "help") and callable(li.help):
                        li.help(this_pars)


def print_functions(obj):
    """
    Print the functions in this object.
    Ignores private methods and help().
    If object doesn't have any public methods
    will print nothing.
    """
    func_list = []
    for name in dir(obj):
        if name.startswith("_") or name == "help":
            continue
        func = getattr(obj, name)
        if callable(func):
            func_list.append(func)

    if len(func_list) > 0:
        print("Methods:")
        for func in func_list:
            print(f"  {func.__name__}{signature(func)}")
        print()


def ra2sex(ra):
    """
    Convert an RA in degrees to a string in sexagesimal format.
    """
    if ra < 0 or ra > 360:
        raise ValueError("RA out of range.")
    ra /= 15.0  # convert to hours
    return f"{int(ra):02d}:{int((ra % 1) * 60):02d}:{((ra % 1) * 60) % 1 * 60:05.2f}"


def dec2sex(dec):
    """
    Convert a Dec in degrees to a string in sexagesimal format.
    """
    if dec < -90 or dec > 90:
        raise ValueError("Dec out of range.")
    return (
        f"{int(dec):+03d}:{int((dec % 1) * 60):02d}:{((dec % 1) * 60) % 1 * 60:04.1f}"
    )


def ra2deg(ra):
    """
    Convert the input right ascension into a float of decimal degrees.
    The input can be a string (with hour angle units) or a float (degree units!).

    Parameters
    ----------
    ra: scalar float or str
        Input RA (right ascension).
        Can be given in decimal degrees or in sexagesimal string (in hours!)
        Example 1: 271.3
        Example 2: 18:23:21.1

    Returns
    -------
    ra: scalar float
        The RA as a float, in decimal degrees

    """
    if type(ra) == str:
        c = SkyCoord(ra=ra, dec=0, unit=(u.hourangle, u.degree))
        ra = c.ra.value  # output in degrees
    else:
        ra = float(ra)

    if not 0.0 < ra < 360.0:
        raise ValueError(f"Value of RA ({ra}) is outside range (0 -> 360).")

    return ra


def dec2deg(dec):
    """
    Convert the input right ascension into a float of decimal degrees.
    The input can be a string (with hour angle units) or a float (degree units!).

    Parameters
    ----------
    dec: scalar float or str
        Input declination.
        Can be given in decimal degrees or in sexagesimal string (in degrees as well)
        Example 1: +33.21 (northern hemisphere)
        Example 2: -22.56 (southern hemisphere)
        Example 3: +12.34.56.7

    Returns
    -------
    dec: scalar float
        The declination as a float, in decimal degrees

    """
    if type(dec) == str:
        c = SkyCoord(ra=0, dec=dec, unit=(u.degree, u.degree))
        dec = c.dec.value  # output in degrees
    else:
        dec = float(dec)

    if not -90.0 < dec < 90.0:
        raise ValueError(f"Value of dec ({dec}) is outside range (-90 -> +90).")

    return dec


def date2jd(date):
    """
    Parse a string or datetime object into a Julian Date (JD) float.
    If string, will parse using dateutil.parser.parse.
    If datetime, will convert to UTC or add that timezone if is naive.
    If given as float, will just return it as a float.

    Parameters
    ----------
    date: float or string or datetime
        The input date or datetime object.

    Returns
    -------
    jd: scalar float
        The Julian Date associated with the input date.

    """
    if isinstance(date, datetime):
        t = date
    elif isinstance(date, str):
        t = dateutil.parser.parse(date)
    else:
        return float(date)

    if t.tzinfo is None:  # naive datetime (no timezone)
        # turn a naive datetime into a UTC datetime
        t = t.replace(tzinfo=timezone.utc)
    else:  # non naive (has timezone)
        t = t.astimezone(timezone.utc)

    return Time(t).jd


def unit_convert_bytes(units):
    """
    Convert a number of bytes into another unit.
    Can choose "kb", "mb", or "gb", which will return
    the appropriate number of bytes in that unit.
    If "bytes" or any other string, will return 1,
    i.e., no conversion.
    """
    if units.endswith("s"):
        units = units[:-1]

    return {
        "byte": 1,
        "kb": 1024,
        "mb": 1024**2,
        "gb": 1024**3,
    }.get(units.lower(), 1)


def is_scalar(value):
    """
    Check if a value is a scalar (string or not has __len__).
    Returns True if a scalar, False if not.
    """
    if isinstance(value, str) or not hasattr(value, "__len__"):
        return True
    else:
        return False
