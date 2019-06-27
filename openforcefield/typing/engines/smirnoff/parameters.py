#!/usr/bin/env python

#=============================================================================================
# MODULE DOCSTRING
#=============================================================================================
"""
Parameter handlers for the SMIRNOFF force field engine

This file contains standard parameter handlers for the SMIRNOFF force field engine.
These classes implement the object model for self-contained parameter assignment.
New pluggable handlers can be created by creating subclasses of :class:`ParameterHandler`.

"""

__all__ = [
    'SMIRNOFFSpecError',
    'IncompatibleParameterError',
    'UnassignedValenceParameterException',
    'UnassignedBondParameterException',
    'UnassignedAngleParameterException',
    'NonbondedMethod',
    'ParameterList',
    'ParameterType',
    'ParameterHandler',
    'ConstraintHandler',
    'BondHandler',
    'AngleHandler',
    'ProperTorsionHandler',
    'ImproperTorsionHandler',
    'vdWHandler'
]


#=============================================================================================
# GLOBAL IMPORTS
#=============================================================================================

import copy
from collections import OrderedDict
from enum import Enum
import functools
import inspect
import logging

from simtk import openmm, unit

from openforcefield.utils import attach_units,  \
    extract_serialized_units_from_dict, ToolkitUnavailableException, MessageException, \
    check_units_are_compatible, object_to_quantity
from openforcefield.topology import ValenceDict, ImproperDict
from openforcefield.typing.chemistry import ChemicalEnvironment
from openforcefield.utils import IncompatibleUnitError
from openforcefield.utils.collections import ValidatedList


#=============================================================================================
# CONFIGURE LOGGER
#=============================================================================================

logger = logging.getLogger(__name__)


#======================================================================
# CUSTOM EXCEPTIONS
#======================================================================

class SMIRNOFFSpecError(MessageException):
    """
    Exception for when data is noncompliant with the SMIRNOFF data specification.
    """
    pass


class IncompatibleParameterError(MessageException):
    """
    Exception for when a set of parameters is scientifically/technically incompatible with another
    """
    pass


class UnassignedValenceParameterException(Exception):
    """Exception raised when there are valence terms for which a ParameterHandler can't find parameters."""
    pass


class UnassignedBondParameterException(UnassignedValenceParameterException):
    """Exception raised when there are bond terms for which a ParameterHandler can't find parameters."""
    pass


class UnassignedAngleParameterException(UnassignedValenceParameterException):
    """Exception raised when there are angle terms for which a ParameterHandler can't find parameters."""
    pass


class UnassignedProperTorsionParameterException(UnassignedValenceParameterException):
    """Exception raised when there are proper torsion terms for which a ParameterHandler can't find parameters."""
    pass


#======================================================================
# ENUM TYPES
#======================================================================

class NonbondedMethod(Enum):
    """
    An enumeration of the nonbonded methods
    """
    NoCutoff = 0
    CutoffPeriodic = 1
    CutoffNonPeriodic = 2
    Ewald = 3
    PME = 4


#======================================================================
# PARAMETER ATTRIBUTES
#======================================================================

# TODO: Think about adding attrs to the dependencies and inherit from attr.ib
class ParameterAttribute:
    """A descriptor for ``ParameterType`` attributes.

    The descriptors allows associating to the parameter a default value,
    which makes the attribute optional, a unit, and a custom converter.

    Because we may want to have ``None`` as a default value, required
    attributes have the ``default`` set to the special type ``UNDEFINED``.

    Converters can be both static or instance functions/methods with
    respective signatures

    converter(value): -> converted_value
    converter(instance, parameter_attribute, value): -> converted_value

    A decorator syntax is available (see example below).

    Parameters
    ----------
    default : object, optional
        When specified, the descriptor makes this attribute optional by
        attaching a default value to it.
    unit : simtk.unit.Quantity, optional
        When specified, only quantities with compatible units are allowed
        to be set, and string expressions are automatically parsed into a
        ``Quantity``.
    converter : callable, optional
        An optional function that can be used to convert values before
        setting the attribute.

    See Also
    --------
    IndexedParameterAttribute
        A parameter attribute with multiple terms.

    Examples
    -------

    Create a parameter type with an optional and a required attribute.

    >>> class MyParameter:
    ...     attr_required = ParameterAttribute()
    ...     attr_optional = ParameterAttribute(default=2)
    ...
    >>> my_par = MyParameter()

    Even without explicit assignment, the default value is returned.

    >>> my_par.attr_optional
    2

    If you try to access an attribute without setting it first, an
    exception is raised.

    >>> my_par.attr_required
    Traceback (most recent call last):
    ...
    AttributeError: 'MyParameter' object has no attribute '_attr_required'

    The attribute allow automatic conversion and validation of units.

    >>> from simtk import unit
    >>> class MyParameter:
    ...     attr_quantity = ParameterAttribute(unit=unit.angstrom)
    ...
    >>> my_par = MyParameter()
    >>> my_par.attr_quantity = '1.0 * nanometer'
    >>> my_par.attr_quantity
    Quantity(value=1.0, unit=nanometer)
    >>> my_par.attr_quantity = 3.0
    Traceback (most recent call last):
    ...
    openforcefield.utils.utils.IncompatibleUnitError: attr_quantity=3.0 dimensionless should have units of angstrom

    You can attach a custom converter to an attribute.

    >>> class MyParameter:
    ...     # Both strings and integers convert nicely to floats with float().
    ...     attr_all_to_float = ParameterAttribute(converter=float)
    ...     attr_int_to_float = ParameterAttribute()
    ...     @attr_int_to_float.converter
    ...     def attr_int_to_float(self, attr, value):
    ...         # This converter converts only integers to float
    ...         # and raise an exception for the other types.
    ...         if isinstance(value, int):
    ...             return float(value)
    ...         elif not isinstance(value, float):
    ...             raise TypeError(f"Cannot convert '{value}' to float")
    ...         return value
    ...
    >>> my_par = MyParameter()

    attr_all_to_float accepts and convert to float both strings and integers

    >>> my_par.attr_all_to_float = 1
    >>> my_par.attr_all_to_float
    1.0
    >>> my_par.attr_all_to_float = '2.0'
    >>> my_par.attr_all_to_float
    2.0

    The custom converter associated to attr_int_to_float converts only integers instead.
    >>> my_par.attr_int_to_float = 3
    >>> my_par.attr_int_to_float
    3.0
    >>> my_par.attr_int_to_float = '4.0'
    Traceback (most recent call last):
    ...
    TypeError: Cannot convert '4.0' to float

    """

    class UNDEFINED:
        """Custom type used by ``ParameterAttribute`` to differentiate between ``None`` and undeclared default."""
        pass

    def __init__(self, default=UNDEFINED, unit=None, converter=None):
        self.default = default
        self._unit = unit
        self._converter = converter

    def __set_name__(self, owner, name):
        self._name = '_' + name

    def __get__(self, instance, owner):
        if instance is None:
            # This is called from the class. Return the descriptor object.
            return self

        try:
            return getattr(instance, self._name)
        except AttributeError:
            # The attribute has not initialized. Check if there's a default.
            if self.default is ParameterAttribute.UNDEFINED:
                raise
            return self.default

    def __set__(self, instance, value):
        # Convert and validate the value.
        value = self._convert_and_validate(instance, value)
        setattr(instance, self._name, value)

    def converter(self, converter):
        """Create a new ParameterAttribute with an associated converter.

        This is meant to be used as a decorator (see main examples).
        """
        return self.__class__(default=self.default, converter=converter)

    def _convert_and_validate(self, instance, value):
        """Convert to Quantity, validate units, and call custom converter."""
        # The default value is always allowed.
        if self._is_valid_default(value):
            return value
        # Convert and validate units.
        value = self._validate_units(value)
        # Call the custom converter before setting the value.
        value = self._call_converter(value, instance)
        return value

    def _is_valid_default(self, value):
        """Return True if this is a defined default value."""
        return self.default is not ParameterAttribute.UNDEFINED and value == self.default

    def _validate_units(self, value):
        """Convert strings expressions to Quantity and validate the units if requested."""
        if self._unit is not None:
            # Convert eventual strings to Quantity objects.
            value = object_to_quantity(value)

            # Check if units are compatible.
            try:
                if not self._unit.is_compatible(value.unit):
                    raise IncompatibleUnitError(f'{self._name[1:]}={value} should have units of {self._unit}')
            except AttributeError:
                # This is not a Quantity object.
                raise IncompatibleUnitError(f'{self._name[1:]}={value} should have units of {self._unit}')
        return value

    def _call_converter(self, value, instance):
        """Correctly calls static and instance converters."""
        if self._converter is not None:
            try:
                # Static function.
                return self._converter(value)
            except TypeError:
                # Instance method.
                return self._converter(instance, self, value)
        return value


class IndexedParameterAttribute(ParameterAttribute):
    """The attribute of a parameter with an unspecified number of terms.

    Some parameters can be associated to multiple terms, For example,
    torsions have parameters such as k1, k2, ..., and ``IndexedParameterAttribute``
    can be used to encapsulate the sequence of terms.

    The only substantial difference with ``ParameterAttribute`` is that
    only sequences are supported as values and converters and units are
    checked on each element of the sequence.

    Currently, the descriptor makes the sequence immutable. This is to
    avoid that an element of the sequence could be set without being
    properly validated. In the future, the data could be wrapped in a
    safe list that would safely allow mutability.

    Parameters
    ----------
    default : object, optional
        When specified, the descriptor makes this attribute optional by
        attaching a default value to it.
    unit : simtk.unit.Quantity, optional
        When specified, only sequences of quantities with compatible units
        are allowed to be set.
    converter : callable, optional
        An optional function that can be used to validate and cast each
        element of the sequence before setting the attribute.

    See Also
    --------
    ParameterAttribute
        A simple parameter attribute.

    Examples
    --------

    Create an optional indexed attribute with unit of angstrom.

    >>> from simtk import unit
    >>> class MyParameter:
    ...     length = IndexedParameterAttribute(default=None, unit=unit.angstrom)
    ...
    >>> my_par = MyParameter()
    >>> my_par.length is None
    True

    Strings are parsed into Quantity objects.

    >>> my_par.length = ['1 * angstrom', 0.5 * unit.nanometer]
    >>> my_par.length[0]
    Quantity(value=1, unit=angstrom)

    Similarly, custom converters work as with ``ParameterAttribute``, but
    they are used to validate each value in the sequence.

    >>> class MyParameter:
    ...     attr_indexed = IndexedParameterAttribute(converter=float)
    ...
    >>> my_par = MyParameter()
    >>> my_par.attr_indexed = [1, '1.0', '1e-2', 4.0]
    >>> my_par.attr_indexed
    [1.0, 1.0, 0.01, 4.0]

    """

    def _convert_and_validate(self, instance, value):
        """Overwrite ParameterAttribute._convert_and_validate to make the value a ValidatedList."""
        # The default value is always allowed.
        if self._is_valid_default(value):
            return value

        # We push the converters into a ValidatedList so that we can make
        # sure that elements are validated correctly when they are modified
        # after their initialization.
        # ValidatedList expects converters that take the value as a single
        # argument so we create a partial function with the instance assigned.
        static_converter = functools.partial(self._call_converter, instance=instance)
        value = ValidatedList(value, converter=[self._validate_units, static_converter])

        return value


class _ParameterAttributeInitializer:
    """A base class for ``ParameterType`` and ``ParameterHandler`` objects.

    Encapsulate shared code of ``ParameterType`` and ``ParameterHandler``.
    In particular, this base class provides an ``__init__`` method that
    automatically initialize the attributes defined through the ``ParameterAttribute``
    and ``IndexedParameterAttribute`` descriptors, as well as handling
    cosmetic attributes.

    See Also
    --------
    ParameterAttribute
        A simple parameter attribute.
    IndexedParameterAttribute
        A parameter attribute with multiple terms.

    """

    def __init__(self, allow_cosmetic_attributes=False, **kwargs):
        """
        Initialize parameter and cosmetic attributes.

        Parameters
        ----------
        allow_cosmetic_attributes : bool optional. Default = False
            Whether to permit non-spec kwargs ("cosmetic attributes").
            If True, non-spec kwargs will be stored as an attribute of
            this parameter which can be accessed and written out. Otherwise,
            an exception will be raised.

        """
        # A list that may be populated to record the cosmetic attributes
        # read from a SMIRNOFF data source.
        self._cosmetic_attribs = []

        # Do not modify the original data.
        smirnoff_data = copy.deepcopy(kwargs)

        # Check for indexed attributes and stack them into a list.
        # Keep track of how many indexed attribute we find to make sure they all have the same length.
        indexed_attr_lengths = {}
        for attrib_basename in self._get_indexed_parameter_attributes().keys():
            index = 1
            while True:
                attrib_w_index = '{}{}'.format(attrib_basename, index)

                # Exit the while loop if the indexed attribute is not given.
                try:
                    attrib_w_index_value = smirnoff_data[attrib_w_index]
                except KeyError:
                    break

                # Check if this is the first iteration.
                if index == 1:
                    # Check if this attribute has been specified with and without index.
                    if attrib_basename in smirnoff_data:
                        err_msg = (f"The attribute '{attrib_basename}' has been specified "
                                   f"with and without index: '{attrib_w_index}'")
                        raise TypeError(err_msg)

                    # Otherwise create the list object.
                    smirnoff_data[attrib_basename] = list()

                # Append the new value to the list.
                smirnoff_data[attrib_basename].append(attrib_w_index_value)

                # Remove the indexed attribute from the kwargs as it will
                # be exposed only as an element of the list.
                del smirnoff_data[attrib_w_index]
                index += 1

            # Update the lengths with this attribute (if it was found).
            if index > 1:
                indexed_attr_lengths[attrib_basename] = len(smirnoff_data[attrib_basename])

        # Raise an error if we there are different indexed
        # attributes with a different number of terms.
        if len(set(indexed_attr_lengths.values())) > 1:
            raise TypeError('The following indexed attributes have '
                            f'different lengths: {indexed_attr_lengths}')

        # Check for missing required arguments.
        given_attributes = set(smirnoff_data.keys())
        required_attributes = set(self._get_required_parameter_attributes().keys())
        missing_attributes = required_attributes.difference(given_attributes)
        if len(missing_attributes) != 0:
            msg = (f"{self.__class__} require the following missing parameters: {sorted(missing_attributes)}."
                   f" Defined kwargs are {sorted(smirnoff_data.keys())}")
            raise SMIRNOFFSpecError(msg)

        # Finally, set attributes of this ParameterType and handle cosmetic attributes.
        allowed_attributes = set(self._get_parameter_attributes().keys())
        for key, val in smirnoff_data.items():
            if key in allowed_attributes:
                setattr(self, key, val)
            # Handle all unknown kwargs as cosmetic so we can write them back out
            elif allow_cosmetic_attributes:
                self.add_cosmetic_attribute(key, val)
            else:
                msg = (f"Unexpected kwarg ({key}: {val})  passed to {self.__class__} constructor. "
                        "If this is a desired cosmetic attribute, consider setting "
                        "'allow_cosmetic_attributes=True'")
                raise SMIRNOFFSpecError(msg)

    def to_dict(self, discard_cosmetic_attributes=False):
        """
        Convert this object to dict format.

        The returning dictionary contains all the ``ParameterAttribute``
        and ``IndexedParameterAttribute`` as well as cosmetic attributes
        if ``discard_cosmetic_attributes`` is ``False``.

        Parameters
        ----------
        discard_cosmetic_attributes : bool, optional. Default = False
            Whether to discard non-spec attributes of this object

        Returns
        -------
        smirnoff_dict : dict
            The SMIRNOFF-compliant dict representation of this object.

        """
        # Make a list of all attribs that should be included in the
        # returned dict (call list() to make a copy). We discard
        # optional attributes that are set to None defaults.
        attribs_to_return = list(self._get_defined_parameter_attributes().keys())

        # Start populating a dict of the attribs.
        indexed_attribs = set(self._get_indexed_parameter_attributes().keys())
        smirnoff_dict = OrderedDict()

        # If attribs_to_return is ordered here, that will effectively be an informal output ordering
        for attrib_name in attribs_to_return:
            attrib_value = getattr(self, attrib_name)

            if attrib_name in indexed_attribs:
                for idx, val in enumerate(attrib_value):
                    smirnoff_dict[attrib_name + str(idx+1)] = val
            else:
                smirnoff_dict[attrib_name] = attrib_value

        # Serialize cosmetic attributes.
        if not(discard_cosmetic_attributes):
            for cosmetic_attrib in self._cosmetic_attribs:
                smirnoff_dict[cosmetic_attrib] = getattr(self, '_' + cosmetic_attrib)

        return smirnoff_dict

    def add_cosmetic_attribute(self, attr_name, attr_value):
        """
        Add a cosmetic attribute to this object.

        This attribute will not have a functional effect on the object
        in the Open Force Field toolkit, but can be written out during
        output.

        .. warning :: The API for modifying cosmetic attributes is experimental
        and may change in the future (see issue #338).

        Parameters
        ----------
        attr_name : str
            Name of the attribute to define for this object.
        attr_value : str
            The value of the attribute to define for this object.

        """
        setattr(self, '_'+attr_name, attr_value)
        self._cosmetic_attribs.append(attr_name)

    def delete_cosmetic_attribute(self, attr_name):
        """
        Delete a cosmetic attribute from this object.

        .. warning :: The API for modifying cosmetic attributes is experimental
        and may change in the future (see issue #338).

        Parameters
        ----------
        attr_name : str
            Name of the cosmetic attribute to delete.
        """
        # TODO: Can we handle this by overriding __delattr__ instead?
        #  Would we also need to override __del__ as well to cover both deletation methods?
        delattr(self, '_'+attr_name)
        self._cosmetic_attribs.remove(attr_name)

    @classmethod
    def _get_parameter_attributes(cls, filter=None):
        """Return all the attributes of the parameters.

        This is constructed dynamically by introspection gathering all
        the descriptors that are instances of the ParameterAttribute class.
        Parent classes of the parameter types are inspected as well.

        Note that since Python 3.6 the order of the class attribute definition
        is preserved (see PEP 520) so this function will return the attribute
        in their declaration order.

        Parameters
        ----------
        filter : Callable, optional
            An optional function with signature filter(ParameterAttribute) -> bool.
            If specified, only attributes for which this functions returns
            True are returned.

        Returns
        -------
        parameter_attributes : Dict[str, ParameterAttribute]
            A map from the name of the controlled parameter to the
            ParameterAttribute descriptor handling it.

        Examples
        --------
        >>> parameter_attributes = ParameterType._get_parameter_attributes()
        >>> sorted(parameter_attributes.keys())
        ['id', 'parent_id', 'smirks']
        >>> isinstance(parameter_attributes['id'], ParameterAttribute)
        True

        """
        # If no filter is specified, get all the parameters.
        if filter is None:
            filter = lambda x: True

        # Go through MRO and retrieve also parents descriptors. The function
        # inspect.getmembers() automatically resolves the MRO, but it also
        # sorts the attribute alphabetically by name. Here we want the order
        # to be the same as the declaration order, which is guaranteed by PEP 520,
        # starting from the parent class.
        parameter_attributes = OrderedDict((name, descriptor) for c in reversed(inspect.getmro(cls))
                                           for name, descriptor in c.__dict__.items()
                                           if isinstance(descriptor, ParameterAttribute) and filter(descriptor))
        return parameter_attributes

    @classmethod
    def _get_indexed_parameter_attributes(cls):
        """Shortcut to retrieve only IndexedParameterAttributes."""
        return cls._get_parameter_attributes(filter=lambda x: isinstance(x, IndexedParameterAttribute))

    @classmethod
    def _get_required_parameter_attributes(cls):
        """Shortcut to retrieve only required ParameterAttributes."""
        return cls._get_parameter_attributes(filter=lambda x: x.default is x.UNDEFINED)

    @classmethod
    def _get_optional_parameter_attributes(cls):
        """Shortcut to retrieve only required ParameterAttributes."""
        return cls._get_parameter_attributes(filter=lambda x: x.default is not x.UNDEFINED)

    def _get_defined_parameter_attributes(self):
        """Returns all the attributes except for the optional attributes that have None default value.

        This returns first the required attributes and then the defined optional
        attribute in their respective declaration order.
        """
        required = self._get_required_parameter_attributes()
        optional = self._get_optional_parameter_attributes()
        # Filter the optional parameters that are set to their default.
        optional = OrderedDict((name, descriptor) for name, descriptor in optional.items()
                               if not(descriptor.default is None and getattr(self, name) == descriptor.default))
        required.update(optional)
        return required


#======================================================================
# PARAMETER TYPE/LIST
#======================================================================

# We can't actually make this derive from dict, because it's possible for the user to change SMIRKS
# of parameters already in the list, which would cause the ParameterType object's SMIRKS and
# the dictionary key's SMIRKS to be out of sync.
class ParameterList(list):
    """
    Parameter list that also supports accessing items by SMARTS string.

    .. warning :: This API is experimental and subject to change.

    """

    # TODO: Make this faster by caching SMARTS -> index lookup?

    # TODO: Override __del__ to make sure we don't remove root atom type

    # TODO: Allow retrieval by `id` as well

    def __init__(self, input_parameter_list=None):
        """
        Initialize a new ParameterList, optionally providing a list of ParameterType objects
        to initially populate it.

        Parameters
        ----------
        input_parameter_list: list[ParameterType], default=None
            A pre-existing list of ParameterType-based objects. If None, this ParameterList
            will be initialized empty.
        """
        super().__init__()

        input_parameter_list = input_parameter_list or []
        # TODO: Should a ParameterList only contain a single kind of ParameterType?
        for input_parameter in input_parameter_list:
            self.append(input_parameter)

    def append(self, parameter):
        """
        Add a ParameterType object to the end of the ParameterList

        Parameters
        ----------
        parameter : a ParameterType object

        """
        # TODO: Ensure that newly added parameter is the same type as existing?
        super().append(parameter)

    def extend(self, other):
        """
        Add a ParameterList object to the end of the ParameterList

        Parameters
        ----------
        other : a ParameterList

        """
        if not isinstance(other, ParameterList):
            msg = 'ParameterList.extend(other) expected instance of ParameterList, ' \
                  'but received {} (type {}) instead'.format(other, type(other))
            raise TypeError(msg)
        # TODO: Check if other ParameterList contains the same ParameterTypes?
        super().extend(other)

    def index(self, item):
        """
        Get the numerical index of a ParameterType object or SMIRKS in this ParameterList. Raises ValueError
        if the item is not found.

        Parameters
        ----------
        item : ParameterType object or str
            The parameter or SMIRKS to look up in this ParameterList

        Returns
        -------
        index : int
            The index of the found item
        """
        if isinstance(item, ParameterType):
            return super().index(item)
        else:
            for parameter in self:
                if parameter.smirks == item:
                    return self.index(parameter)
            raise IndexError('SMIRKS {item} not found in ParameterList'.format(item=item))

    def insert(self, index, parameter):
        """
        Add a ParameterType object as if this were a list

        Parameters
        ----------
        index : int
            The numerical position to insert the parameter at
        parameter : a ParameterType object
            The parameter to insert
        """
        # TODO: Ensure that newly added parameter is the same type as existing?
        super().insert(index, parameter)

    def __delitem__(self, item):
        """
        Delete item by index or SMIRKS.

        Parameters
        ----------
        item : str or int
            SMIRKS or numerical index of item in this ParameterList
        """
        if type(item) is int:
            index = item
        else:
            # Try to find by SMIRKS
            index = self.index(item)
        super().__delitem__(index)

    def __getitem__(self, item):
        """
        Retrieve item by index or SMIRKS

        Parameters
        ----------
        item : str or int
            SMIRKS or numerical index of item in this ParameterList
        """
        if type(item) is int:
            index = item
        elif type(item) is slice:
            index = item
        else:
            index = self.index(item)
        return super().__getitem__(index)


    # TODO: Override __setitem__ and __del__ to ensure we can slice by SMIRKS as well

    def __contains__(self, item):
        """Check to see if either Parameter or SMIRKS is contained in parameter list.


        Parameters
        ----------
        item : str
            SMIRKS of item in this ParameterList
        """
        if isinstance(item, str):
            # Special case for SMIRKS strings
            if item in [result.smirks for result in self]:
                return True
        # Fall back to traditional access
        return list.__contains__(self, item)

    def to_list(self, discard_cosmetic_attributes=True):
        """
        Render this ParameterList to a normal list, serializing each ParameterType object in it to dict.

        Parameters
        ----------

        discard_cosmetic_attributes : bool, optional. Default = True
            Whether to discard non-spec attributes of each ParameterType object.

        Returns
        -------
        parameter_list : List[dict]
            A serialized representation of a ParameterList, with each ParameterType it contains converted to dict.
        """
        parameter_list = list()

        for parameter in self:
            parameter_dict = parameter.to_dict(discard_cosmetic_attributes=discard_cosmetic_attributes)
            parameter_list.append(parameter_dict)

        return parameter_list


# TODO: Rename to better reflect role as parameter base class?
class ParameterType(_ParameterAttributeInitializer):
    """
    Base class for SMIRNOFF parameter types.

    This base class provides utilities to create new parameter types. See
    the below for examples of how to do this.

    .. warning :: This API is experimental and subject to change.

    Attributes
    ----------
    smirks : str
        The SMIRKS pattern that this parameter matches.
    id : str or None
        An optional identifier for the parameter.
    parent_id : str or None
        Optionally, the identifier of the parameter of which this parameter
        is a specialization.

    See Also
    --------
    ParameterAttribute
    IndexedParameterAttribute

    Examples
    --------

    This class allows to define new parameter types by just listing its
    attributes. In the example below, ``_VALENCE_TYPE`` AND ``_ELEMENT_NAME``
    are used for the validation of the SMIRKS pattern associated to the
    parameter and the automatic serialization/deserialization into a ``dict``.

    >>> class MyBondParameter(ParameterType):
    ...     _VALENCE_TYPE = 'Bond'
    ...     _ELEMENT_NAME = 'Bond'
    ...     length = ParameterAttribute(unit=unit.angstrom)
    ...     k = ParameterAttribute(unit=unit.kilocalorie_per_mole / unit.angstrom**2)
    ...

    The parameter automatically inherits the required smirks attribute
    from ``ParameterType``. Associating a ``unit`` to a ``ParameterAttribute``
    cause the attribute to accept only values in compatible units and to
    parse string expressions.

    >>> my_par = MyBondParameter(
    ...     smirks='[*:1]-[*:2]',
    ...     length='1.01 * angstrom',
    ...     k=5 * unit.kilocalorie_per_mole / unit.angstrom**2
    ... )
    >>> my_par.length
    Quantity(value=1.01, unit=angstrom)
    >>> my_par.k = 3.0 * unit.gram
    Traceback (most recent call last):
    ...
    openforcefield.utils.utils.IncompatibleUnitError: k=3.0 g should have units of kilocalorie/(angstrom**2*mole)

    Each attribute can be made optional by specifying a default value,
    and you can attach a converter function by passing a callable as an
    argument or through the decorator syntax.

    >>> class MyParameterType(ParameterType):
    ...     _VALENCE_TYPE = 'Atom'
    ...     _ELEMENT_NAME = 'Atom'
    ...
    ...     attr_optional = ParameterAttribute(default=2)
    ...     attr_all_to_float = ParameterAttribute(converter=float)
    ...     attr_int_to_float = ParameterAttribute()
    ...
    ...     @attr_int_to_float.converter
    ...     def attr_int_to_float(self, attr, value):
    ...         # This converter converts only integers to floats
    ...         # and raise an exception for the other types.
    ...         if isinstance(value, int):
    ...             return float(value)
    ...         elif not isinstance(value, float):
    ...             raise TypeError(f"Cannot convert '{value}' to float")
    ...         return value
    ...
    >>> my_par = MyParameterType(smirks='[*:1]', attr_all_to_float='3.0', attr_int_to_float=1)
    >>> my_par.attr_optional
    2
    >>> my_par.attr_all_to_float
    3.0
    >>> my_par.attr_int_to_float
    1.0

    The float() function can convert strings to integers, but our custom
    converter forbids it

    >>> my_par.attr_all_to_float = '2.0'
    >>> my_par.attr_int_to_float = '4.0'
    Traceback (most recent call last):
    ...
    TypeError: Cannot convert '4.0' to float

    Parameter attributes that can be indexed can be handled with the
    ``IndexedParameterAttribute``. These support unit validation and
    converters exactly as ``ParameterAttribute``s, but the validation/conversion
    is performed for each indexed attribute.

    >>> class MyTorsionType(ParameterType):
    ...     _VALENCE_TYPE = 'ProperTorsion'
    ...     _ELEMENT_NAME = 'Proper'
    ...     periodicity = IndexedParameterAttribute(converter=int)
    ...     k = IndexedParameterAttribute(unit=unit.kilocalorie_per_mole)
    ...
    >>> my_par = MyTorsionType(
    ...     smirks='[*:1]-[*:2]-[*:3]-[*:4]',
    ...     periodicity1=2,
    ...     k1=5 * unit.kilocalorie_per_mole,
    ...     periodicity2='3',
    ...     k2=6 * unit.kilocalorie_per_mole,
    ... )
    >>> my_par.periodicity
    [2, 3]

    """

    # ChemicalEnvironment valence type string expected by SMARTS string for this Handler
    _VALENCE_TYPE = None
    # The string mapping to this ParameterType in a SMIRNOFF data source
    _ELEMENT_NAME = None

    # Parameter attributes shared among all parameter types.
    smirks = ParameterAttribute()
    id = ParameterAttribute(default=None)
    parent_id = ParameterAttribute(default=None)

    @smirks.converter
    def smirks(self, attr, smirks):
        # Validate the SMIRKS string to ensure it matches the expected
        # parameter type, raising an exception if it is invalid or doesn't
        # tag a valid set of atoms.

        # TODO: Make better switch using toolkit registry after refactoring ChemicalEnvironment module.
        from openforcefield.utils.toolkits import OPENEYE_AVAILABLE, RDKIT_AVAILABLE
        toolkit = None
        if OPENEYE_AVAILABLE:
            toolkit = 'openeye'
        elif RDKIT_AVAILABLE:
            toolkit = 'rdkit'
        if toolkit is None:
            raise ToolkitUnavailableException(
                "Validating SMIRKS required either the OpenEye Toolkit or the RDKit."
                " Unable to find either.")

        # TODO: Add check to make sure we can't make tree non-hierarchical
        #       This would require parameter type knows which ParameterList it belongs to
        ChemicalEnvironment.validate(
            smirks, ensure_valence_type=self._VALENCE_TYPE, toolkit=toolkit)
        return smirks

    def __init__(self, smirks, allow_cosmetic_attributes=False, **kwargs):
        """
        Create a ParameterType.

        Parameters
        ----------
        smirks : str
            The SMIRKS match for the provided parameter type.
        allow_cosmetic_attributes : bool optional. Default = False
            Whether to permit non-spec kwargs ("cosmetic attributes"). If True, non-spec kwargs will be stored as
            an attribute of this parameter which can be accessed and written out. Otherwise an exception will
            be raised.

        """
        # This is just to make smirks a required positional argument.
        kwargs['smirks']  = smirks
        super().__init__(allow_cosmetic_attributes=allow_cosmetic_attributes, **kwargs)

    def __repr__(self):
        ret_str = '<{} with '.format(self.__class__.__name__)
        for attr, val in self.to_dict().items():
            ret_str += f'{attr}: {val}  '
        ret_str += '>'
        return ret_str


#======================================================================
# PARAMETER HANDLERS
#
# The following classes are Handlers that know how to create Force
# subclasses and add them to a System that is being created. Each Handler
# class must define three methods:
# 1) a constructor which takes as input hierarchical dictionaries of data
#    conformant to the SMIRNOFF spec;
# 2) a create_force() method that constructs the Force object and adds it
#    to the System; and
# 3) a labelForce() method that provides access to which terms are applied
#    to which atoms in specified mols.
#======================================================================

# TODO: Should we have a parameter handler registry?


class ParameterHandler:
    """Base class for parameter handlers.

    Parameter handlers are configured with some global parameters for a given section. They may also contain a
    :class:`ParameterList` populated with :class:`ParameterType` objects if they are responsile for assigning
    SMIRKS-based parameters.

    .. warning

       Parameter handler objects can only belong to a single :class:`ForceField` object.
       If you need to create a copy to attach to a different :class:`ForceField` object, use ``create_copy()``.

    .. warning :: This API is experimental and subject to change.

    """

    _TAGNAME = None  # str of section type handled by this ParameterHandler (XML element name for SMIRNOFF XML representation)
    _INFOTYPE = None  # container class with type information that will be stored in self._parameters
    _OPENMMTYPE = None  # OpenMM Force class (or None if no equivalent)
    _DEPENDENCIES = None  # list of ParameterHandler classes that must precede this, or None
    _REQUIRED_SPEC_ATTRIBS = ['version'] # list of kwargs that must be present during handler initialization
    _DEFAULT_SPEC_ATTRIBS = {}  # dict of tag-level attributes and their default values
    _OPTIONAL_SPEC_ATTRIBS = []  # list of non-required attributes that can be defined on initialization
    _INDEXED_ATTRIBS = []  # list of parameter attribs that will have consecutive numerical suffixes starting at 1
    _REQUIRE_UNITS = {}  # dict of {header attrib : unit } for input checking
    _ATTRIBS_TO_TYPE = {} # dict of attribs that must be cast to a specific type to be interpreted correctly
    _KWARGS = [] # Kwargs to catch when create_force is called
    _SMIRNOFF_VERSION_INTRODUCED = 0.0  # the earliest version of SMIRNOFF spec that supports this ParameterHandler
    _SMIRNOFF_VERSION_DEPRECATED = None  # if deprecated, the first SMIRNOFF version number it is no longer used
    _MIN_SUPPORTED_SECTION_VERSION = 0.3
    _MAX_SUPPORTED_SECTION_VERSION = 0.3


    def __init__(self, allow_cosmetic_attributes=False, skip_version_check=False, **kwargs):
        """
        Initialize a ParameterHandler, optionally with a list of parameters and other kwargs.

        Parameters
        ----------
        allow_cosmetic_attributes : bool, optional. Default = False
            Whether to permit non-spec kwargs. If True, non-spec kwargs will be stored as attributes of this object
            and can be accessed and modified. Otherwise an exception will be raised if a non-spec kwarg is encountered.
        skip_version_check: bool, optional. Default = False
            If False, the SMIRNOFF section version will not be checked, and the ParameterHandler will be initialized
            with version set to _MAX_SUPPORTED_SECTION_VERSION.
        **kwargs : dict
            The dict representation of the SMIRNOFF data source

        """
        if 'version' in self._REQUIRED_SPEC_ATTRIBS:
            if not 'version' in kwargs:
                if skip_version_check:
                    kwargs['version'] = self._MAX_SUPPORTED_SECTION_VERSION
                else:
                    raise SMIRNOFFSpecError(f"Missing version while trying to construct {self.__class__}. "
                                            f"0.3 SMIRNOFF spec requires each parameter section to have its own "
                                            f"version.")
            version = kwargs['version']
            self._check_section_version_compatibility(version)

        self._cosmetic_attribs = []  # list of cosmetic header attributes to remember and optionally write out

        # Ensure that all required attribs are present
        for reqd_attrib in self._REQUIRED_SPEC_ATTRIBS:
            if not reqd_attrib in kwargs:
                msg = "{} requires {} as a parameter during initialization, however this is not " \
                      "provided. Defined kwargs are {}".format(self.__class__,
                                                               reqd_attrib,
                                                               list(kwargs.keys()))
                raise SMIRNOFFSpecError(msg)

        # list of ParameterType objects (also behaves like an OrderedDict where keys are SMARTS)
        self._parameters = ParameterList()

        # Handle all the unknown kwargs as cosmetic so we can write them back out
        allowed_header_attribs = self._REQUIRED_SPEC_ATTRIBS + \
                                 list(self._DEFAULT_SPEC_ATTRIBS.keys()) + \
                                 self._OPTIONAL_SPEC_ATTRIBS


        # Check for indexed attribs
        for attrib_basename in self._INDEXED_ATTRIBS:
            # attrib_unit_key = attrib_basename + '_unit'

            index = 1
            attrib_w_index = '{}{}'.format(attrib_basename, index)
            while attrib_w_index in kwargs:
                # As long as we keep finding higher-indexed entries for
                # this attrib, add them to the expected arguments
                allowed_header_attribs.append(attrib_w_index)

                if attrib_basename in self._REQUIRE_UNITS:
                    self._REQUIRE_UNITS[attrib_w_index] = self._REQUIRE_UNITS[attrib_basename]
                if attrib_basename in self._ATTRIBS_TO_TYPE:
                    self._ATTRIBS_TO_TYPE[attrib_w_index] = self._ATTRIBS_TO_TYPE[attrib_basename]


        # Check for attribs that need to be casted to specific types
        for attrib, type_to_cast in self._ATTRIBS_TO_TYPE.items():
            if attrib in kwargs:
                kwargs[attrib] = type_to_cast(kwargs[attrib])

        smirnoff_data = kwargs


        # Add default values to smirnoff_data if they're not already there
        for default_key, default_val in self._DEFAULT_SPEC_ATTRIBS.items():
            if not (default_key in kwargs):
                smirnoff_data[default_key] = default_val

        # Perform unit compatibility checks
        for key in smirnoff_data.keys():
            if key in self._REQUIRE_UNITS:
                context = f"In {self.__class__}'s __init__ function. "
                check_units_are_compatible(key, smirnoff_data[key], self._REQUIRE_UNITS[key], context=context)

        element_name = None
        if self._INFOTYPE is not None:
            element_name = self._INFOTYPE._ELEMENT_NAME

        for key, val in smirnoff_data.items():
            # We don't initialize parameters here, only ParameterHandler attributes
            if key == element_name:
                continue
            elif key in allowed_header_attribs:
                attr_name = '_' + key
                # TODO: create @property.setter here if attrib requires unit
                setattr(self, attr_name, val)
            elif allow_cosmetic_attributes:
                self.add_cosmetic_attribute(key, val)
                #self._cosmetic_attribs.append(key)
                #attr_name = '_' + key
                #setattr(self, attr_name, val)


            else:
                raise SMIRNOFFSpecError("Unexpected kwarg {} passed to {} constructor. If this is "
                                        "a desired cosmetic attribute, consider setting "
                                        "'allow_cosmetic_attributes=True'".format(key, self.__class__))



    def _check_section_version_compatibility(self, version):
        """
        Raise a parsing exception if the given section version is incompatible with this ParameterHandler class.

        Parameters
        ----------
        version : str
            The SMIRNOFF section version being read.

        Raises
        ------
        SMIRNOFFVersionError if an incompatible version is passed in.

        """
        import packaging.version
        from openforcefield.typing.engines.smirnoff import SMIRNOFFVersionError
        # Use PEP-440 compliant version number comparison, if requested
        if (
                packaging.version.parse(str(version)) >
                packaging.version.parse(str(self._MAX_SUPPORTED_SECTION_VERSION))

                ) or (
                packaging.version.parse(str(version)) <
                packaging.version.parse(str(self._MIN_SUPPORTED_SECTION_VERSION))
                ):

            raise SMIRNOFFVersionError(
                'SMIRNOFF offxml file was written with version {}, but this version of ForceField only supports '
                'version {} to version {}'.format(version,
                                                  self._MIN_SUPPORTED_SECTION_VERSION,
                                                  self._MAX_SUPPORTED_SECTION_VERSION))


    def _add_parameters(self, section_dict, allow_cosmetic_attributes=False):
        """
        Extend the ParameterList in this ParameterHandler using a SMIRNOFF data source.

        Parameters
        ----------
        section_dict : dict
            The dict representation of a SMIRNOFF data source containing parameters to att to this ParameterHandler
        allow_cosmetic_attributes : bool, optional. Default = False
            Whether to allow non-spec fields in section_dict. If True, non-spec kwargs will be stored as an
            attribute of the parameter. If False, non-spec kwargs will raise an exception.

        """
        unitless_kwargs, attached_units = extract_serialized_units_from_dict(section_dict)
        smirnoff_data = attach_units(unitless_kwargs, attached_units)

        element_name = None
        if self._INFOTYPE is not None:
            element_name = self._INFOTYPE._ELEMENT_NAME

        for key, val in smirnoff_data.items():
            # Skip sections that aren't the parameter list
            if key != element_name:
                continue
            # If there are multiple parameters, this will be a list. If there's just one, make it a list
            if not (isinstance(val, list)):
                val = [val]
            # If we're reading the parameter list, iterate through and attach units to
            # each parameter_dict, then use it to initialize a ParameterType
            for unitless_param_dict in val:
                param_dict = attach_units(unitless_param_dict, attached_units)
                new_parameter = self._INFOTYPE(**param_dict,
                                               allow_cosmetic_attributes=allow_cosmetic_attributes)
                self._parameters.append(new_parameter)

    @property
    def parameters(self):
        """The ParameterList that holds this ParameterHandler's parameter objects"""
        return self._parameters

    def add_cosmetic_attribute(self, attr_name, attr_value):
        """
        Add a cosmetic attribute to this ParameterHandler object. This attribute will not have a functional effect
        on the object in the Open Force Field toolkit, but can be written out during output.

        Parameters
        ----------
        attr_name : str
            Name of the attribute to define for this ParameterType object.
        attr_value : str
            The value of the attribute to define for this ParameterType object.
        """
        setattr(self, '_'+attr_name, attr_value)
        self._cosmetic_attribs.append(attr_name)

    def delete_cosmetic_attribute(self, attr_name):
        """
        Delete a cosmetic attribute from this ParameterHandler object.

        Parameters
        ----------
        attr_name : str
            Name of the attribute to delete.
        """
        # TODO: Can we handle this by overriding __delattr__ instead?
        #  Would we also need to override __del__ as well to cover both deletation methods?
        delattr(self, '_'+attr_name)
        self._cosmetic_attribs.remove(attr_name)



    # TODO: Do we need to return these, or can we handle this internally
    @property
    def known_kwargs(self):
        """List of kwargs that can be parsed by the function.
        """
        # TODO: Should we use introspection to inspect the function signature instead?
        return set(self._KWARGS)


    #@classmethod
    def check_parameter_compatibility(self, parameter_kwargs):
        """
        Check to make sure that the fields requiring defined units are compatible with the required units for the
        Parameters handled by this ParameterHandler

        Parameters
        ----------
        parameter_kwargs: dict
            The dict that will be used to construct the ParameterType

        Raises
        ------
        Raises a ValueError if the parameters are incompatible.
        """
        for key in parameter_kwargs:
            if key in self._REQUIRE_UNITS:
                reqd_unit = self._REQUIRE_UNITS[key]
                val = parameter_kwargs[key]
                context = f"In {self.__class__}'s check_parameter_compatibility. "
                check_units_are_compatible(key, val, reqd_unit, context=context)


    def check_handler_compatibility(self, handler_kwargs):
        """
        Checks if a set of kwargs used to create a ParameterHandler are compatible with this ParameterHandler. This is
        called if a second handler is attempted to be initialized for the same tag.

        Parameters
        ----------
        handler_kwargs : dict
            The kwargs that would be used to construct

        Raises
        ------
        IncompatibleParameterError if handler_kwargs are incompatible with existing parameters.
        """
        pass

    # TODO: Can we ensure SMIRKS and other parameters remain valid after manipulation?
    def add_parameter(self, parameter_kwargs):
        """Add a parameter to the forcefield, ensuring all parameters are valid.

        Parameters
        ----------
        parameter_kwargs : dict
            The kwargs to pass to the ParameterHandler.INFOTYPE (a ParameterType) constructor
        """

        # TODO: Do we need to check for incompatibility with existing parameters?

        # Perform unit compatibility checks
        self.check_parameter_compatibility(parameter_kwargs)
        # Check for correct SMIRKS valence

        new_parameter = self._INFOTYPE(**parameter_kwargs)
        self._parameters.append(new_parameter)

    def get_parameter(self, parameter_attrs):
        """
        Return the parameters in this ParameterHandler that match the parameter_attrs argument

        Parameters
        ----------
        parameter_attrs : dict of {attr: value}
            The attrs mapped to desired values (for example {"smirks": "[*:1]~[#16:2]=,:[#6:3]~[*:4]", "id": "t105"} )

        Returns
        -------
        list of ParameterType objects
            A list of matching ParameterType objects
        """
        # TODO: This is a necessary API point for Lee-Ping's ForceBalance
        pass

    class _Match:
        """Represents a ParameterType which has been matched to
        a given chemical environment.
        """

        @property
        def parameter_type(self):
            """ParameterType: The matched parameter type."""
            return self._parameter_type

        @property
        def environment_match(self):
            """Topology._ChemicalEnvironmentMatch: The environment which matched the type."""
            return self._environment_match

        def __init__(self, parameter_type, environment_match):
            """Constructs a new ParameterHandlerMatch object.

            Parameters
            ----------
            parameter_type: ParameterType
                The matched parameter type.
            environment_match: Topology._ChemicalEnvironmentMatch
                The environment which matched the type.
            """
            self._parameter_type = parameter_type
            self._environment_match = environment_match

    def find_matches(self, entity):
        """Find the elements of the topology/molecule matched by a parameter type.

        Parameters
        ----------
        entity : openforcefield.topology.Topology
            Topology to search.

        Returns
        ---------
        matches : ValenceDict[Tuple[int], ParameterHandler._Match]
            ``matches[particle_indices]`` is the ``ParameterType`` object
            matching the tuple of particle indices in ``entity``.
        """

        # TODO: Right now, this method is only ever called with an entity that is a Topoogy.
        #  Should we reduce its scope and have a check here to make sure entity is a Topology?
        return self._find_matches(entity)

    def _find_matches(self, entity, transformed_dict_cls=ValenceDict):
        """Implement find_matches() and allow using a difference valence dictionary.
                Parameters
        ----------
        entity : openforcefield.topology.Topology
            Topology to search.
        transformed_dict_cls: class
            The type of dictionary to store the matches in. This
            will determine how groups of atom indices are stored
            and accessed (e.g for angles indices should be 0-1-2
            and not 2-1-0).

        Returns
        ---------
        matches : `transformed_dict_cls` of ParameterHandlerMatch
            ``matches[particle_indices]`` is the ``ParameterType`` object
            matching the tuple of particle indices in ``entity``.
        """
        logger.debug('Finding matches for {}'.format(self.__class__.__name__))

        matches = transformed_dict_cls()

        # TODO: There are probably performance gains to be had here
        #       by performing this loop in reverse order, and breaking early once
        #       all environments have been matched.
        for parameter_type in self._parameters:
            matches_for_this_type = {}

            for environment_match in entity.chemical_environment_matches(parameter_type.smirks):
                # Update the matches for this parameter type.
                handler_match = ParameterHandler._Match(parameter_type, environment_match)
                matches_for_this_type[environment_match.topology_atom_indices] = handler_match

            # Update matches of all parameter types.
            matches.update(matches_for_this_type)

            logger.debug('{:64} : {:8} matches'.format(
                parameter_type.smirks, len(matches_for_this_type)))

        logger.debug('{} matches identified'.format(len(matches)))
        return matches

    @staticmethod
    def _assert_correct_connectivity(match, expected_connectivity=None):
        """A more performant version of the `topology.assert_bonded` method
        to ensure that the results of `_find_matches` are valid.

        Raises
        ------
        ValueError
            Raise an exception when the atoms in the match don't have
            the correct connectivity.

        Parameters
        ----------
        match: ParameterHandler._Match
            The match found by `_find_matches`
        connectivity: list of tuple of int, optional
            The expected connectivity of the match (e.g. for a torsion
            expected_connectivity=[(0, 1), (1, 2), (2, 3)]). If `None`,
            a connectivity of [(0, 1), ... (n - 1, n)] is assumed.
        """

        # I'm not 100% sure this is really necessary... but this should do
        # the same checks as the more costly assert_bonded method in the
        # ParameterHandler.create_force methods.
        if expected_connectivity is None:
            expected_connectivity = [(i, i + 1) for i in range(len(match.environment_match.topology_atom_indices) - 1)]

        reference_molecule = match.environment_match.reference_molecule

        for connectivity in expected_connectivity:

            atom_i = match.environment_match.reference_atom_indices[connectivity[0]]
            atom_j = match.environment_match.reference_atom_indices[connectivity[1]]

            reference_molecule.get_bond_between(atom_i, atom_j)

    def assign_parameters(self, topology, system):
        """Assign parameters for the given Topology to the specified System object.

        Parameters
        ----------
        topology : openforcefield.topology.Topology
            The Topology for which parameters are to be assigned.
            Either a new Force will be created or parameters will be appended to an existing Force.
        system : simtk.openmm.System
            The OpenMM System object to add the Force (or append new parameters) to.
        """
        pass

    def postprocess_system(self, topology, system, **kwargs):
        """Allow the force to perform a a final post-processing pass on the System following parameter assignment, if needed.

        Parameters
        ----------
        topology : openforcefield.topology.Topology
            The Topology for which parameters are to be assigned.
            Either a new Force will be created or parameters will be appended to an existing Force.
        system : simtk.openmm.System
            The OpenMM System object to add the Force (or append new parameters) to.
        """
        pass


    def to_dict(self, discard_cosmetic_attributes=False):
        """
        Convert this ParameterHandler to an OrderedDict, compliant with the SMIRNOFF data spec.

        Parameters
        ----------
        discard_cosmetic_attributes : bool, optional. Default = False.
            Whether to discard non-spec parameter and header attributes in this ParameterHandler.

        Returns
        -------
        smirnoff_data : OrderedDict
            SMIRNOFF-spec compliant representation of this ParameterHandler and its internal ParameterList.
        """
        smirnoff_data = OrderedDict()


        # Populate parameter list
        parameter_list = self._parameters.to_list(discard_cosmetic_attributes=discard_cosmetic_attributes)

        # NOTE: This assumes that a ParameterHandler will have just one homogenous ParameterList under it
        if self._INFOTYPE is not None:
            #smirnoff_data[self._INFOTYPE._ELEMENT_NAME] = unitless_parameter_list
            smirnoff_data[self._INFOTYPE._ELEMENT_NAME] = parameter_list


        # Collect the names of handler attributes to return
        header_attribs_to_return = self._REQUIRED_SPEC_ATTRIBS + list(self._DEFAULT_SPEC_ATTRIBS.keys())

        # Check whether the optional attribs are defined, and add them if so
        for key in self._OPTIONAL_SPEC_ATTRIBS:
            attr_key = '_' + key
            if hasattr(self, attr_key):
                header_attribs_to_return.append(key)
        # Add the cosmetic attributes if requested
        if not(discard_cosmetic_attributes):
            header_attribs_to_return += self._cosmetic_attribs


        # Go through the attribs of this ParameterHandler and collect the appropriate values to return
        header_attribute_dict = {}
        for header_attribute in header_attribs_to_return:
            value = getattr(self, '_' + header_attribute)
            header_attribute_dict[header_attribute] = value


        smirnoff_data.update(header_attribute_dict)
        # smirnoff_data.update(output_units)
        return smirnoff_data

    # -------------------------------
    # Utilities for children classes.
    # -------------------------------

    @classmethod
    def _check_all_valence_terms_assigned(cls, assigned_terms, valence_terms,
                                          exception_cls=UnassignedValenceParameterException):
        """Check that all valence terms have been assigned and print a user-friendly error message.

        Parameters
        ----------
        assigned_terms : ValenceDict
            Atom index tuples defining added valence terms.
        valence_terms : Iterable[TopologyAtom] or Iterable[Iterable[TopologyAtom]]
            Atom or atom tuples defining topological valence terms.
        exception_cls : UnassignedValenceParameterException
            A specific exception class to raise to allow catching only specific
            types of errors.

        """

        # Provided there are no duplicates in either list,
        # or something weird like a bond has been added to
        # a torsions list - this should work just fine I think.
        # If we expect either of those assumptions to be incorrect,
        # (i.e len(not_found_terms) > 0) we have bigger issues
        # in the code and should be catching those cases elsewhere!
        # The fact that we graph match all topol molecules to ref
        # molecules should avoid the len(not_found_terms) > 0 case.

        if len(assigned_terms) == len(valence_terms):
            return

        # Convert the valence term to a valence dictionary to make sure
        # the order of atom indices doesn't matter for comparison.
        valence_terms_dict = assigned_terms.__class__()
        for atoms in valence_terms:
            try:
                # valence_terms is a list of TopologyAtom tuples.
                atom_indices = (a.topology_particle_index for a in atoms)
            except TypeError:
                # valence_terms is a list of TopologyAtom.
                atom_indices = (atoms.topology_particle_index,)
            valence_terms_dict[atom_indices] = atoms

        # Check that both valence dictionaries have the same keys (i.e. terms).
        assigned_terms_set = set(assigned_terms.keys())
        valence_terms_set = set(valence_terms_dict.keys())
        unassigned_terms = valence_terms_set.difference(assigned_terms_set)
        not_found_terms = assigned_terms_set.difference(valence_terms_set)

        # Raise an error if there are unassigned terms.
        err_msg = ""

        if len(unassigned_terms) > 0:
            unassigned_str = '\n- '.join([str(x) for x in unassigned_terms])
            err_msg += ("{parameter_handler} was not able to find parameters for the following valence terms:\n"
                        "- {unassigned_str}").format(parameter_handler=cls.__name__,
                                                     unassigned_str=unassigned_str)
        if len(not_found_terms) > 0:
            if err_msg != "":
                err_msg += '\n'
            not_found_str = '\n- '.join([str(x) for x in not_found_terms])
            err_msg += ("{parameter_handler} assigned terms that were not found in the topology:\n"
                        "- {not_found_str}").format(parameter_handler=cls.__name__,
                                                    not_found_str=not_found_str)
        if err_msg != "":
            err_msg += '\n'
            raise exception_cls(err_msg)


#=============================================================================================


class ConstraintHandler(ParameterHandler):
    """Handle SMIRNOFF ``<Constraints>`` tags

    ``ConstraintHandler`` must be applied before ``BondHandler`` and ``AngleHandler``,
    since those classes add constraints for which equilibrium geometries are needed from those tags.

    .. warning :: This API is experimental and subject to change.
    """

    class ConstraintType(ParameterType):
        """A SMIRNOFF constraint type

        .. warning :: This API is experimental and subject to change.
        """
        _VALENCE_TYPE = 'Bond'
        _ELEMENT_NAME = 'Constraint'

        distance = ParameterAttribute(default=None, unit=unit.angstrom)


    _TAGNAME = 'Constraints'
    _INFOTYPE = ConstraintType
    _OPENMMTYPE = None  # don't create a corresponding OpenMM Force class

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def create_force(self, system, topology, **kwargs):
        constraint_matches = self.find_matches(topology)
        for (atoms, constraint_match) in constraint_matches.items():
            # Update constrained atom pairs in topology
            #topology.add_constraint(*atoms, constraint.distance)
            # If a distance is specified (constraint.distance != True), add the constraint here.
            # Otherwise, the equilibrium bond length will be used to constrain the atoms in HarmonicBondHandler
            constraint = constraint_match.parameter_type

            if constraint.distance is None:
                topology.add_constraint(*atoms, True)
            else:
                system.addConstraint(*atoms, constraint.distance)
                topology.add_constraint(*atoms, constraint.distance)

#=============================================================================================


class BondHandler(ParameterHandler):
    """Handle SMIRNOFF ``<Bonds>`` tags

    .. warning :: This API is experimental and subject to change.
    """

    class BondType(ParameterType):
        """A SMIRNOFF bond type

        .. warning :: This API is experimental and subject to change.
        """
        # ChemicalEnvironment valence type string expected by SMARTS string for this Handler
        _VALENCE_TYPE = 'Bond'
        _ELEMENT_NAME = 'Bond'

        # These attributes may be indexed (by integer bond order) if fractional bond orders are used.
        length = ParameterAttribute(unit=unit.angstrom)
        k = ParameterAttribute(unit=unit.kilocalorie_per_mole / unit.angstrom**2)


    _TAGNAME = 'Bonds'  # SMIRNOFF tag name to process
    _INFOTYPE = BondType  # class to hold force type info
    _OPENMMTYPE = openmm.HarmonicBondForce  # OpenMM force class to create
    _DEPENDENCIES = [ConstraintHandler]  # ConstraintHandler must be executed first
    _DEFAULT_SPEC_ATTRIBS = {'potential': 'harmonic',
                             'fractional_bondorder_method': None,
                             'fractional_bondorder_interpolation': 'linear'}
    _INDEXED_ATTRIBS = ['k'] # May be indexed (by integer bond order) if fractional bond orders are used

    def __init__(self, **kwargs):
        # TODO: Do we want a docstring here? If not, check that docstring get inherited from ParameterHandler.
        super().__init__(**kwargs)

    def check_handler_compatibility(self,
                                    other_handler):
        """
        Checks whether this ParameterHandler encodes compatible physics as another ParameterHandler. This is
        called if a second handler is attempted to be initialized for the same tag.

        Parameters
        ----------
        other_handler : a ParameterHandler object
            The handler to compare to.

        Raises
        ------
        IncompatibleParameterError if handler_kwargs are incompatible with existing parameters.
        """
        string_attrs_to_compare = ['potential', 'fractional_bondorder_method', 'fractional_bondorder_interpolation']

        for string_attr in string_attrs_to_compare:
            this_val = getattr(self, '_' + string_attr)
            other_val = getattr(other_handler, '_' + string_attr)
            if this_val != other_val:
                raise IncompatibleParameterError(
                    "{} values are not identical. "
                    "(handler value: {}, incompatible value: {}".format(
                        string_attr, this_val, other_val))

    def create_force(self, system, topology, **kwargs):
        # Create or retrieve existing OpenMM Force object
        # TODO: The commented line below should replace the system.getForce search
        #force = super(BondHandler, self).create_force(system, topology, **kwargs)
        existing = [system.getForce(i) for i in range(system.getNumForces())]
        existing = [f for f in existing if type(f) == self._OPENMMTYPE]
        if len(existing) == 0:
            force = self._OPENMMTYPE()
            system.addForce(force)
        else:
            force = existing[0]

        # Add all bonds to the system.
        bond_matches = self.find_matches(topology)

        skipped_constrained_bonds = 0  # keep track of how many bonds were constrained (and hence skipped)
        for (topology_atom_indices, bond_match) in bond_matches.items():
            # Get corresponding particle indices in Topology
            #particle_indices = tuple([ atom.particle_index for atom in atoms ])

            # Ensure atoms are actually bonded correct pattern in Topology
            ParameterHandler._assert_correct_connectivity(bond_match)
            # topology.assert_bonded(atoms[0], atoms[1])
            bond_params = bond_match.parameter_type
            match = bond_match.environment_match

            # Compute equilibrium bond length and spring constant.
            bond = match.reference_molecule.get_bond_between(*match.reference_atom_indices)

            if bond.fractional_bond_order is None:
                [k, length] = [bond_params.k, bond_params.length]
            else:
                # Interpolate using fractional bond orders
                # TODO: Do we really want to allow per-bond specification of interpolation schemes?
                order = bond.fractional_bond_order
                if self.fractional_bondorder_interpolation == 'interpolate-linear':
                    k = bond_params.k[0] + (bond_params.k[1] - bond_params.k[0]) * (order - 1.)
                    length = bond_params.length[0] + (
                        bond_params.length[1] - bond_params.length[0]) * (order - 1.)
                else:
                    raise Exception(
                        "Partial bondorder treatment {} is not implemented.".
                        format(self.fractional_bondorder_method))

            is_constrained = topology.is_constrained(*topology_atom_indices)

            # Handle constraints.
            if is_constrained:
                # Atom pair is constrained; we don't need to add a bond term.
                skipped_constrained_bonds += 1
                # Check if we need to add the constraint here to the equilibrium bond length.
                if is_constrained is True:
                    # Mark that we have now assigned a specific constraint distance to this constraint.
                    topology.add_constraint(*topology_atom_indices, length)
                    # Add the constraint to the System.
                    system.addConstraint(*topology_atom_indices, length)
                    #system.addConstraint(*particle_indices, length)
                continue

            # Add harmonic bond to HarmonicBondForce
            force.addBond(*topology_atom_indices, length, k)

        logger.info('{} bonds added ({} skipped due to constraints)'.format(
            len(bond_matches) - skipped_constrained_bonds, skipped_constrained_bonds))

        # Check that no topological bonds are missing force parameters.
        valence_terms = [list(b.atoms) for b in topology.topology_bonds]
        self._check_all_valence_terms_assigned(assigned_terms=bond_matches, valence_terms=valence_terms,
                                               exception_cls=UnassignedBondParameterException)


#=============================================================================================


class AngleHandler(ParameterHandler):
    """Handle SMIRNOFF ``<AngleForce>`` tags

    .. warning :: This API is experimental and subject to change.
    """

    class AngleType(ParameterType):
        """A SMIRNOFF angle type.

        .. warning :: This API is experimental and subject to change.
        """
        _VALENCE_TYPE = 'Angle'  # ChemicalEnvironment valence type string expected by SMARTS string for this Handler
        _ELEMENT_NAME = 'Angle'

        angle = ParameterAttribute(unit=unit.degree)
        k = ParameterAttribute(unit=unit.kilocalorie_per_mole / unit.degree**2)


    _TAGNAME = 'Angles'  # SMIRNOFF tag name to process
    _INFOTYPE = AngleType  # class to hold force type info
    _OPENMMTYPE = openmm.HarmonicAngleForce  # OpenMM force class to create
    _DEPENDENCIES = [ConstraintHandler]  # ConstraintHandler must be executed first
    _DEFAULT_SPEC_ATTRIBS = {'potential': 'harmonic'}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def check_handler_compatibility(self,
                                    other_handler):
        """
        Checks whether this ParameterHandler encodes compatible physics as another ParameterHandler. This is
        called if a second handler is attempted to be initialized for the same tag.

        Parameters
        ----------
        other_handler : a ParameterHandler object
            The handler to compare to.

        Raises
        ------
        IncompatibleParameterError if handler_kwargs are incompatible with existing parameters.
        """
        string_attrs_to_compare = ['potential']

        for string_attr in string_attrs_to_compare:
            this_val = getattr(self, '_' + string_attr)
            other_val = getattr(other_handler, '_' + string_attr)
            if this_val != other_val:
                raise IncompatibleParameterError(
                    "{} values are not identical. "
                    "(handler value: {}, incompatible value: {}".format(
                        string_attr, this_val, other_val))




    def create_force(self, system, topology, **kwargs):
        #force = super(AngleHandler, self).create_force(system, topology, **kwargs)
        existing = [system.getForce(i) for i in range(system.getNumForces())]
        existing = [f for f in existing if type(f) == self._OPENMMTYPE]
        if len(existing) == 0:
            force = self._OPENMMTYPE()
            system.addForce(force)
        else:
            force = existing[0]

        # Add all angles to the system.
        angle_matches = self.find_matches(topology)
        skipped_constrained_angles = 0  # keep track of how many angles were constrained (and hence skipped)
        for (atoms, angle_match) in angle_matches.items():
            # Ensure atoms are actually bonded correct pattern in Topology
            # for (i, j) in [(0, 1), (1, 2)]:
            #     topology.assert_bonded(atoms[i], atoms[j])
            ParameterHandler._assert_correct_connectivity(angle_match)

            if topology.is_constrained(
                    atoms[0], atoms[1]) and topology.is_constrained(
                        atoms[1], atoms[2]) and topology.is_constrained(
                            atoms[0], atoms[2]):
                # Angle is constrained; we don't need to add an angle term.
                skipped_constrained_angles += 1
                continue

            angle = angle_match.parameter_type
            force.addAngle(*atoms, angle.angle, angle.k)

        logger.info('{} angles added ({} skipped due to constraints)'.format(
            len(angle_matches) - skipped_constrained_angles,
            skipped_constrained_angles))

        # Check that no topological angles are missing force parameters
        self._check_all_valence_terms_assigned(assigned_terms=angle_matches,
                                               valence_terms=list(topology.angles),
                                               exception_cls=UnassignedAngleParameterException)


#=============================================================================================


class ProperTorsionHandler(ParameterHandler):
    """Handle SMIRNOFF ``<ProperTorsionForce>`` tags

    .. warning :: This API is experimental and subject to change.
    """

    class ProperTorsionType(ParameterType):
        """A SMIRNOFF torsion type for proper torsions.

        .. warning :: This API is experimental and subject to change.
        """

        _VALENCE_TYPE = 'ProperTorsion'
        _ELEMENT_NAME = 'Proper'

        periodicity = IndexedParameterAttribute(converter=int)
        phase = IndexedParameterAttribute(unit=unit.degree)
        k = IndexedParameterAttribute(unit=unit.kilocalorie_per_mole)
        idivf = IndexedParameterAttribute(default=None, converter=float)


    _TAGNAME = 'ProperTorsions'  # SMIRNOFF tag name to process
    _INFOTYPE = ProperTorsionType  # info type to store
    _OPENMMTYPE = openmm.PeriodicTorsionForce  # OpenMM force class to create
    _DEFAULT_SPEC_ATTRIBS = {'potential': 'k*(1+cos(periodicity*theta-phase))',
                             'default_idivf': 'auto'}
    _INDEXED_ATTRIBS = ['k', 'phase', 'periodicity', 'idivf']


    def __init__(self, **kwargs):

        # NOTE: We do not want to overwrite idivf values here! If they're missing from the ParameterType
        # dictionary, that means they should be set to defualt _AT SYSTEM CREATION TIME_. The user may
        # change that default to a different value than it is now. The solution here will be to leave
        # those idivfX values uninitialized and deal with it during system creation

        super().__init__(**kwargs)
        self.validate_parameters()


    def check_handler_compatibility(self,
                                    other_handler):
        """
        Checks whether this ParameterHandler encodes compatible physics as another ParameterHandler. This is
        called if a second handler is attempted to be initialized for the same tag.

        Parameters
        ----------
        other_handler : a ParameterHandler object
            The handler to compare to.

        Raises
        ------
        IncompatibleParameterError if handler_kwargs are incompatible with existing parameters.
        """
        float_attrs_to_compare = []
        string_attrs_to_compare = ['potential']

        if self._default_idivf == 'auto':
            string_attrs_to_compare.append('default_idivf')
        else:
            float_attrs_to_compare.append('default_idivf')

        for float_attr in float_attrs_to_compare:
            this_val = getattr(self, '_' + float_attr)
            other_val = getattr(other_handler, '_' + float_attr)
            if abs(this_val - other_val) > 1.e-6:
                raise IncompatibleParameterError(
                    "Difference between '{}' values is beyond allowed tolerance {}. "
                    "(handler value: {}, incompatible value: {}".format(
                        float_attr, self._SCALETOL, this_val, other_val))

        for string_attr in string_attrs_to_compare:
            this_val = getattr(self, '_' + string_attr)
            other_val = getattr(other_handler, '_' + string_attr)
            if this_val != other_val:
                raise IncompatibleParameterError(
                    "{} values are not identical. "
                    "(handler value: {}, incompatible value: {}".format(
                        string_attr, this_val, other_val))

    def validate_parameters(self):
        supported_torsion_potentials = ['k*(1+cos(periodicity*theta-phase))']
        if self._potential not in supported_torsion_potentials:
            raise SMIRNOFFSpecError(f"ProperTorsionHandler given 'potential' value of "
                                    f"'{self._potential}'. Supported options are {supported_torsion_potentials}.")

    def create_force(self, system, topology, **kwargs):
        self.validate_parameters()
        #force = super(ProperTorsionHandler, self).create_force(system, topology, **kwargs)
        existing = [system.getForce(i) for i in range(system.getNumForces())]
        existing = [f for f in existing if type(f) == self._OPENMMTYPE]
        if len(existing) == 0:
            force = self._OPENMMTYPE()
            system.addForce(force)
        else:
            force = existing[0]
        # Add all proper torsions to the system.
        torsion_matches = self.find_matches(topology)

        for (atom_indices, torsion_match) in torsion_matches.items():
            # Ensure atoms are actually bonded correct pattern in Topology
            ParameterHandler._assert_correct_connectivity(torsion_match)

            torsion = torsion_match.parameter_type

            for (periodicity, phase, k, idivf) in zip(torsion.periodicity,
                                               torsion.phase, torsion.k, torsion.idivf):
                if idivf == 'auto':
                    # TODO: Implement correct "auto" behavior
                    raise NotImplementedError("The OpenForceField toolkit hasn't implemented "
                                              "support for the torsion `idivf` value of 'auto'")

                force.addTorsion(atom_indices[0], atom_indices[1],
                                 atom_indices[2], atom_indices[3], periodicity,
                                 phase, k/idivf)

        logger.info('{} torsions added'.format(len(torsion_matches)))

        # Check that no topological torsions are missing force parameters

        # I can see the apeal of these kind of methods as an 'absolute' check
        # that things have gone well, but I think just making sure that the
        # reference molecule has been fully parametrised should have the same
        # effect! It would be good to eventually refactor things so that everything
        # is focused on the single unique molecules, and then simply just cloned
        # onto the system. It seems like John's proposed System object would do
        # exactly this.
        self._check_all_valence_terms_assigned(assigned_terms=torsion_matches,
                                               valence_terms=list(topology.propers),
                                               exception_cls=UnassignedProperTorsionParameterException)


class ImproperTorsionHandler(ParameterHandler):
    """Handle SMIRNOFF ``<ImproperTorsionForce>`` tags

    .. warning :: This API is experimental and subject to change.
    """

    class ImproperTorsionType(ParameterType):
        """A SMIRNOFF torsion type for improper torsions.

        .. warning :: This API is experimental and subject to change.
        """
        _VALENCE_TYPE = 'ImproperTorsion'
        _ELEMENT_NAME = 'Improper'

        periodicity = IndexedParameterAttribute(converter=int)
        phase = IndexedParameterAttribute(unit=unit.degree)
        k = IndexedParameterAttribute(unit=unit.kilocalorie_per_mole)
        idivf = IndexedParameterAttribute(default=None, converter=float)


    _TAGNAME = 'ImproperTorsions'  # SMIRNOFF tag name to process
    _INFOTYPE = ImproperTorsionType  # info type to store
    _OPENMMTYPE = openmm.PeriodicTorsionForce  # OpenMM force class to create
    _OPTIONAL_SPEC_ATTRIBS = ['potential', 'default_idivf']
    _DEFAULT_SPEC_ATTRIBS = {'potential': 'k*(1+cos(periodicity*theta-phase))',
                             'default_idivf': 'auto'}
    _INDEXED_ATTRIBS = ['k', 'phase', 'periodicity', 'idivf']



    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.validate_parameters()

    def check_handler_compatibility(self,
                                    other_handler):
        """
        Checks whether this ParameterHandler encodes compatible physics as another ParameterHandler. This is
        called if a second handler is attempted to be initialized for the same tag.

        Parameters
        ----------
        other_handler : a ParameterHandler object
            The handler to compare to.

        Raises
        ------
        IncompatibleParameterError if handler_kwargs are incompatible with existing parameters.
        """
        float_attrs_to_compare = []
        string_attrs_to_compare = ['potential']

        if self._default_idivf == 'auto':
            string_attrs_to_compare.append('default_idivf')
        else:
            float_attrs_to_compare.append('default_idivf')

        for float_attr in float_attrs_to_compare:
            this_val = getattr(self, '_' + float_attr)
            other_val = getattr(other_handler, '_' + float_attr)
            if abs(this_val - other_val) > 1.e-6:
                raise IncompatibleParameterError(
                    "Difference between '{}' values is beyond allowed tolerance {}. "
                    "(handler value: {}, incompatible value: {}".format(
                        float_attr, self._SCALETOL, this_val, other_val))

        for string_attr in string_attrs_to_compare:
            this_val = getattr(self, '_' + string_attr)
            other_val = getattr(other_handler, '_' + string_attr)
            if this_val != other_val:
                raise IncompatibleParameterError(
                    "{} values are not identical. "
                    "(handler value: {}, incompatible value: {}".format(
                        string_attr, this_val, other_val))


    def validate_parameters(self):
        supported_torsion_potentials = ['k*(1+cos(periodicity*theta-phase))']
        if self._potential not in supported_torsion_potentials:
            raise SMIRNOFFSpecError(f"ImproperTorsionHandler given 'potential' value of "
                                    f"'{self._potential}'. Supported options are {supported_torsion_potentials}.")

    def find_matches(self, entity):
        """Find the improper torsions in the topology/molecule matched by a parameter type.

        Parameters
        ----------
        entity : openforcefield.topology.Topology
            Topology to search.

        Returns
        ---------
        matches : ImproperDict[Tuple[int], ParameterHandler._Match]
            ``matches[atom_indices]`` is the ``ParameterType`` object
            matching the 4-tuple of atom indices in ``entity``.

        """
        return self._find_matches(entity, transformed_dict_cls=ImproperDict)

    def create_force(self, system, topology, **kwargs):
        #force = super(ImproperTorsionHandler, self).create_force(system, topology, **kwargs)
        #force = super().create_force(system, topology, **kwargs)
        self.validate_parameters()
        existing = [system.getForce(i) for i in range(system.getNumForces())]
        existing = [
            f for f in existing if type(f) == openmm.PeriodicTorsionForce
        ]
        if len(existing) == 0:
            force = openmm.PeriodicTorsionForce()
            system.addForce(force)
        else:
            force = existing[0]

        # Add all improper torsions to the system
        improper_matches = self.find_matches(topology)
        for (atom_indices, improper_match) in improper_matches.items():
            # Ensure atoms are actually bonded correct pattern in Topology
            # For impropers, central atom is atom 1
            # for (i, j) in [(0, 1), (1, 2), (1, 3)]:
            #     topology.assert_bonded(atom_indices[i], atom_indices[j])
            ParameterHandler._assert_correct_connectivity(improper_match, [(0, 1), (1, 2), (1, 3)])

            improper = improper_match.parameter_type

            # TODO: This is a lazy hack. idivf should be set according to the ParameterHandler's default_idivf attrib
            if improper.idivf is None:
                improper.idivf = [3 for item in improper.k]
            # Impropers are applied in three paths around the trefoil having the same handedness
            for (improper_periodicity, improper_phase, improper_k, improper_idivf) in zip(improper.periodicity,
                                               improper.phase, improper.k, improper.idivf):
                # TODO: Implement correct "auto" behavior
                if improper_idivf == 'auto':
                    improper_idivf = 3
                    logger.warning("The OpenForceField toolkit hasn't implemented "
                                   "support for the torsion `idivf` value of 'auto'."
                                   "Currently assuming a value of '3' for impropers.")
                # Permute non-central atoms
                others = [atom_indices[0], atom_indices[2], atom_indices[3]]
                # ((0, 1, 2), (1, 2, 0), and (2, 0, 1)) are the three paths around the trefoil
                for p in [(others[i], others[j], others[k]) for (i, j, k) in [(0, 1, 2), (1, 2, 0), (2, 0, 1)]]:
                    # The torsion force gets added three times, since the k is divided by three
                    force.addTorsion(atom_indices[1], p[0], p[1], p[2],
                                     improper_periodicity, improper_phase, improper_k/improper_idivf)
        logger.info(
            '{} impropers added, each applied in a six-fold trefoil'.format(
                len(improper_matches)))


class vdWHandler(ParameterHandler):
    """Handle SMIRNOFF ``<vdW>`` tags

    .. warning :: This API is experimental and subject to change.
    """

    class vdWType(ParameterType):
        """A SMIRNOFF vdWForce type.

        .. warning :: This API is experimental and subject to change.
        """
        _VALENCE_TYPE = 'Atom'  # ChemicalEnvironment valence type expected for SMARTS
        _ELEMENT_NAME = 'Atom'

        epsilon = ParameterAttribute(unit=unit.kilocalorie_per_mole)
        sigma = ParameterAttribute(default=None, unit=unit.angstrom)
        rmin_half = ParameterAttribute(default=None, unit=unit.angstrom)

        def __init__(self, **kwargs):
            sigma = kwargs.get('sigma', None)
            rmin_half = kwargs.get('rmin_half', None)
            if (sigma is None) and (rmin_half is None):
                raise SMIRNOFFSpecError("Either sigma or rmin_half must be specified.")
            if (sigma is not None) and (rmin_half is not None):
                raise SMIRNOFFSpecError(
                    "BOTH sigma and rmin_half cannot be specified simultaneously."
                )

            super().__init__(**kwargs)


    _TAGNAME = 'vdW'  # SMIRNOFF tag name to process
    _INFOTYPE = vdWType  # info type to store
    _OPENMMTYPE = openmm.NonbondedForce  # OpenMM force class to create
    # _KWARGS = ['ewaldErrorTolerance',
    #            'useDispersionCorrection',
    #            'usePbc'] # Kwargs to catch when create_force is called
    _REQUIRE_UNITS = {'switch_width': unit.angstrom,
                      'cutoff': unit.angstrom}
    _DEFAULT_SPEC_ATTRIBS = {
        'potential': 'Lennard-Jones-12-6',
        'combining_rules': 'Lorentz-Berthelot',
        'scale12': 0.0,
        'scale13': 0.0,
        'scale14': 0.5,
        'scale15': 1.0,
        #'pme_tolerance': 1.e-5,
        'switch_width': 1.0 * unit.angstroms,
        'cutoff': 9.0 * unit.angstroms,
        'method': 'cutoff',
    }

    _ATTRIBS_TO_TYPE = {'scale12': float,
                        'scale13': float,
                        'scale14': float,
                        'scale15': float
                        }

    # TODO: Is this necessary? It's used in check_compatibility but could be hard-coded.
    _SCALETOL = 1e-5


    def __init__(self, **kwargs):

        super().__init__(**kwargs)
        self._validate_parameters()

    # TODO: These properties are a fast hack and should be replaced by something better
    @property
    def potential(self):
        """The potential used to model van der Waals interactions"""
        return self._potential

    @potential.setter
    def potential(self, other):
        """The potential used to model van der Waals interactions"""
        valid_potentials = ['Lennard-Jones-12-6']
        if other not in valid_potentials:
            raise IncompatibleParameterError(f"Attempted to set vdW potential to {other}. Expected "
                                             f"one of {valid_potentials}")
        self._potential = other

    @property
    def combining_rules(self):
        """The combining_rules used to model van der Waals interactions"""
        return self._combining_rules

    @combining_rules.setter
    def combining_rules(self, other):
        """The combining_rules used to model van der Waals interactions"""
        valid_combining_ruless = ['Lorentz-Berthelot']
        if other not in valid_combining_ruless:
            raise IncompatibleParameterError(f"Attempted to set vdW combining_rules to {other}. Expected "
                                             f"one of {valid_combining_ruless}")
        self._method = other

    @property
    def method(self):
        """The method used to handle long-range van der Waals interactions"""
        return self._method

    @method.setter
    def method(self, other):
        """The method used to handle long-range van der Waals interactions"""
        valid_methods = ['cutoff', 'PME']
        if other not in valid_methods:
            raise IncompatibleParameterError(f"Attempted to set vdW method to {other}. Expected "
                                             f"one of {valid_methods}")
        self._method = other

    @property
    def cutoff(self):
        """The cutoff used for long-range van der Waals interactions"""
        return self._cutoff

    @cutoff.setter
    def cutoff(self, other):
        """The cutoff used for long-range van der Waals interactions"""
        unit_to_check = self._REQUIRE_UNITS['cutoff']
        if not unit_to_check.unit_is_compatible(other.unit):
            raise IncompatibleParameterError(
                f"Attempted to set vdW cutoff to {other}, which is not compatible with "
                f"expected unit {unit_to_check}")
        self._cutoff = other

    @property
    def switch_width(self):
        """The switching width used for long-range van der Waals interactions"""
        return self._switch_width

    @switch_width.setter
    def switch_width(self, other):
        """The switching width used for long-range van der Waals interactions"""
        unit_to_check = self._REQUIRE_UNITS['switch_width']
        if not unit_to_check.unit_is_compatible(other.unit):
            raise IncompatibleParameterError(
                f"Attempted to set vdW switch_width to {other}, which is not compatible with "
                f"expected unit {unit_to_check}")
        self._switch_width = other

    def _validate_parameters(self):
        """
        Checks internal attributes, raising an exception if they are configured in an invalid way.
        """
        if self._scale12 != 0.0:
            raise SMIRNOFFSpecError("Current OFF toolkit is unable to handle scale12 values other than 0.0. "
                                    "Specified 1-2 scaling was {}".format(self._scale12))
        if self._scale13 != 0.0:
            raise SMIRNOFFSpecError("Current OFF toolkit is unable to handle scale13 values other than 0.0. "
                                    "Specified 1-3 scaling was {}".format(self._scale13))
        if self._scale15 != 1.0:
            raise SMIRNOFFSpecError("Current OFF toolkit is unable to handle scale15 values other than 1.0. "
                                    "Specified 1-5 scaling was {}".format(self._scale15))


        supported_methods = ['cutoff', 'PME']
        if self._method not in supported_methods:
            raise SMIRNOFFSpecError("The Open Force Field toolkit currently only supports vdW method "
                                    "values of {}. Received unsupported value "
                                    "{}".format(supported_methods, self._method))

        elif self._method == 'cutoff':
            if self._cutoff is None:
                raise SMIRNOFFSpecError("If vdW method is cutoff, a cutoff distance "
                                        "must be provided")

        elif self._method == 'PME':
            if self._cutoff is None:
                raise SMIRNOFFSpecError("If vdW method is PME, a cutoff distance "
                                        "must be provided")

            # if self._pme_tolerance is None:
            #     raise SMIRNOFFSpecError("If PME vdW method is selected, a pme_tolerance value must "
            #                             "be specified.")

        if self._potential != "Lennard-Jones-12-6":
            raise SMIRNOFFSpecError("vdW potential set to {}. Only 'Lennard-Jones-12-6' is currently "
                                    "supported".format(self._potential))


        if self._combining_rules != "Lorentz-Berthelot":
            raise SMIRNOFFSpecError("vdW combining_rules set to {}. Only 'Lorentz-Berthelot' is currently "
                                    "supported".format(self._combining_rules))
        # TODO: Find a better way to set defaults
        # TODO: Validate these values against the supported output types (openMM force kwargs?)
        # TODO: Add conditional logic to assign NonbondedMethod and check compatibility

    def check_handler_compatibility(self,
                                    other_handler):
        """
        Checks whether this ParameterHandler encodes compatible physics as another ParameterHandler. This is
        called if a second handler is attempted to be initialized for the same tag.

        Parameters
        ----------
        other_handler : a ParameterHandler object
            The handler to compare to.

        Raises
        ------
        IncompatibleParameterError if handler_kwargs are incompatible with existing parameters.
        """
        float_attrs_to_compare = ['scale12', 'scale13', 'scale14', 'scale15']
        string_attrs_to_compare = ['potential', 'combining_rules', 'method']
        unit_attrs_to_compare = ['cutoff']

        for float_attr in float_attrs_to_compare:
            this_val = getattr(self, '_' + float_attr)
            other_val = getattr(other_handler, '_' + float_attr)
            if abs(this_val - other_val) > self._SCALETOL:
                raise IncompatibleParameterError(
                    "Difference between '{}' values is beyond allowed tolerance {}. "
                    "(handler value: {}, incompatible value: {}".format(
                        float_attr, self._SCALETOL, this_val, other_val))

        for string_attr in string_attrs_to_compare:
            this_val = getattr(self, '_' + string_attr)
            other_val = getattr(other_handler, '_' + string_attr)
            if this_val != other_val:
                raise IncompatibleParameterError(
                    "{} values are not identical. "
                    "(handler value: {}, incompatible value: {}".format(
                        string_attr, this_val, other_val))

        for unit_attr in unit_attrs_to_compare:
            this_val = getattr(self, '_' + unit_attr)
            other_val = getattr(other_handler, '_' + unit_attr)
            unit_tol = (self._SCALETOL * this_val.unit) # TODO: do we want a different quantity_tol here?
            if abs(this_val - other_val) > unit_tol:
                raise IncompatibleParameterError(
                    "Difference between '{}' values is beyond allowed tolerance {}. "
                    "(handler value: {}, incompatible value: {}".format(
                        unit_attr, unit_tol, this_val, other_val))

    def create_force(self, system, topology, **kwargs):

        self._validate_parameters()

        force = openmm.NonbondedForce()


        # If we're using PME, then the only possible openMM Nonbonded type is LJPME
        if self._method == 'PME':
            # If we're given a nonperiodic box, we always set NoCutoff. Later we'll add support for CutoffNonPeriodic
            if (topology.box_vectors is None):
                force.setNonbondedMethod(openmm.NonbondedForce.NoCutoff)
                # if (topology.box_vectors is None):
                #     raise SMIRNOFFSpecError("If vdW method is  PME, a periodic Topology "
                #                             "must be provided")
            else:
                force.setNonbondedMethod(openmm.NonbondedForce.LJPME)
                force.setCutoffDistance(9. * unit.angstrom)
                force.setEwaldErrorTolerance(1.e-4)

        # If method is cutoff, then we currently support openMM's PME for periodic system and NoCutoff for nonperiodic
        elif self._method == 'cutoff':
            # If we're given a nonperiodic box, we always set NoCutoff. Later we'll add support for CutoffNonPeriodic
            if (topology.box_vectors is None):
                force.setNonbondedMethod(openmm.NonbondedForce.NoCutoff)
            else:
                force.setNonbondedMethod(openmm.NonbondedForce.PME)
                force.setUseDispersionCorrection(True)
                force.setCutoffDistance(self._cutoff)

        system.addForce(force)

        # Iterate over all defined Lennard-Jones types, allowing later matches to override earlier ones.
        atom_matches = self.find_matches(topology)

        # Create all particles.
        for _ in topology.topology_particles:
            force.addParticle(0.0, 1.0, 0.0)

        # Set the particle Lennard-Jones terms.
        for atom_key, atom_match in atom_matches.items():
            atom_idx = atom_key[0]
            ljtype = atom_match.parameter_type
            if ljtype.sigma is None:
                sigma = 2. * ljtype.rmin_half / (2.**(1. / 6.))
            else:
                sigma = ljtype.sigma
            force.setParticleParameters(atom_idx, 0.0, sigma,
                                        ljtype.epsilon)

        # Check that no atoms (n.b. not particles) are missing force parameters.
        self._check_all_valence_terms_assigned(assigned_terms=atom_matches,
                                               valence_terms=list(topology.topology_atoms))

    # TODO: Can we express separate constraints for postprocessing and normal processing?
    def postprocess_system(self, system, topology, **kwargs):
        # Create exceptions based on bonds.
        # TODO: This postprocessing must occur after the ChargeIncrementModelHandler
        # QUESTION: Will we want to do this for *all* cases, or would we ever want flexibility here?
        bond_particle_indices = []

        for topology_molecule in topology.topology_molecules:

            top_mol_particle_start_index = topology_molecule.atom_start_topology_index

            for topology_bond in topology_molecule.bonds:

                top_index_1 = topology_molecule._ref_to_top_index[topology_bond.bond.atom1_index]
                top_index_2 = topology_molecule._ref_to_top_index[topology_bond.bond.atom2_index]

                top_index_1 += top_mol_particle_start_index
                top_index_2 += top_mol_particle_start_index

                bond_particle_indices.append((top_index_1, top_index_2))

        for force in system.getForces():
            # TODO: Should we just store which `Force` object we are adding to and use that instead,
            # to prevent interference with other kinds of forces in the future?
            # TODO: Can we generalize this to allow for `CustomNonbondedForce` implementations too?
            if isinstance(force, openmm.NonbondedForce):
                #nonbonded.createExceptionsFromBonds(bond_particle_indices, self.coulomb14scale, self.lj14scale)

                # TODO: Don't mess with electrostatic scaling here. Have a separate electrostatics handler.
                force.createExceptionsFromBonds(bond_particle_indices, 0.83333,
                                                self._scale14)
                #force.createExceptionsFromBonds(bond_particle_indices, self.coulomb14scale, self._scale14)


class ElectrostaticsHandler(ParameterHandler):
    """Handles SMIRNOFF ``<Electrostatics>`` tags.

    .. warning :: This API is experimental and subject to change.
    """
    _TAGNAME = 'Electrostatics'
    _OPENMMTYPE = openmm.NonbondedForce
    _DEPENDENCIES = [vdWHandler]
    _DEFAULT_SPEC_ATTRIBS = {
        'method': 'PME',
        'scale12': 0.0,
        'scale13': 0.0,
        'scale14': 0.833333,
        'scale15': 1.0,
        #'pme_tolerance': 1.e-5,
        #'switch_width': 8.0 * unit.angstrom, # OpenMM can't support an electrostatics switch
        'switch_width': 0.0 * unit.angstrom,
        'cutoff': 9.0 * unit.angstrom
    }
    _ATTRIBS_TO_TYPE = {'scale12': float,
                        'scale13': float,
                        'scale14': float,
                        'scale15': float
                        }

    _OPTIONAL_SPEC_ATTRIBS = ['cutoff', 'switch_width']

    _SCALETOL = 1e-5

    def __init__(self, **kwargs):

        super().__init__(**kwargs)
        self._validate_parameters()


    @property
    def method(self):
        """The method used to model long-range electrostatic interactions"""
        return self._method

    @method.setter
    def method(self, other):
        """The method used to model long-range electrostatic interactions"""
        valid_methods = ['PME', 'Coulomb', 'reaction-field']
        if other not in valid_methods:
            raise IncompatibleParameterError(f"Attempted to set electrostatics method to {other}. Expected "
                                             f"one of {valid_methods}")
        self._method = other


    @property
    def cutoff(self):
        """The cutoff used for long-range van der Waals interactions"""
        return self._cutoff

    @cutoff.setter
    def cutoff(self, other):
        """The cutoff used for long-range van der Waals interactions"""
        unit_to_check = self._REQUIRE_UNITS['cutoff']
        if not unit_to_check.unit_is_compatible(other.unit):
            raise IncompatibleParameterError(
                f"Attempted to set vdW cutoff to {other}, which is not compatible with "
                f"expected unit {unit_to_check}")
        self._cutoff = other

    @property
    def switch_width(self):
        """The switching width used for long-range electrostatics interactions"""
        return self._switch_width

    @switch_width.setter
    def switch_width(self, other):
        """The switching width used for long-range van der Waals interactions"""
        unit_to_check = self._REQUIRE_UNITS['switch_width']
        if not unit_to_check.unit_is_compatible(other.unit):
            raise IncompatibleParameterError(
                f"Attempted to set vdW switch_width to {other}, which is not compatible with "
                f"expected unit {unit_to_check}")
        self._switch_width = other


    def _validate_parameters(self):
        """
        Checks internal attributes, raising an exception if they are configured in an invalid way.
        """
        if self._scale12 != 0.0:
            raise IncompatibleParameterError("Current OFF toolkit is unable to handle scale12 values other than 0.0. "
                                             "Specified 1-2 scaling was {}".format(self._scale12))
        if self._scale13 != 0.0:
            raise IncompatibleParameterError("Current OFF toolkit is unable to handle scale13 values other than 0.0. "
                                             "Specified 1-3 scaling was {}".format(self._scale13))
        if self._scale15 != 1.0:
            raise IncompatibleParameterError("Current OFF toolkit is unable to handle scale15 values other than 1.0. "
                                    "Specified 1-5 scaling was {}".format(self._scale15))

        supported_methods = ['PME', 'Coulomb'] # 'reaction-field'
        if self._method == 'reaction-field':
            raise IncompatibleParameterError('The Open Force Field toolkit does not currently support reaction-field '
                                             'electrostatics.')

        if not self._method in supported_methods:
            raise IncompatibleParameterError("'method' parameter in Electrostatics tag {} is not a supported "
                                    "option. Valid methods are {}".format(self._method, supported_methods))

        if self._method == 'reaction-field' or self._method == 'PME':
            if self._cutoff is None:
                raise SMIRNOFFSpecError("If Electrostatics method is 'reaction-field' or 'PME', then 'cutoff' must "
                                        "also be specified")

        if self._switch_width != 0.0 * unit.angstrom:
            raise IncompatibleParameterError("The current implementation of the Open Force Field toolkit can not "
                                             "support an electrostatic switching width. Currently only `0.0 angstroms` "
                                             "is supported (SMIRNOFF data specified {})".format(self._switch_width))
    def check_handler_compatibility(self,
                                    other_handler):
        """
        Checks whether this ParameterHandler encodes compatible physics as another ParameterHandler. This is
        called if a second handler is attempted to be initialized for the same tag.

        Parameters
        ----------
        other_handler : a ParameterHandler object
            The handler to compare to.

        Raises
        ------
        IncompatibleParameterError if handler_kwargs are incompatible with existing parameters.
        """
        float_attrs_to_compare = ['scale12', 'scale13', 'scale14', 'scale15']
        string_attrs_to_compare = ['method']
        unit_attrs_to_compare = ['cutoff', 'switch_width']

        for float_attr in float_attrs_to_compare:
            this_val = getattr(self, '_' + float_attr)
            other_val = getattr(other_handler, '_' + float_attr)
            if abs(this_val - other_val) > self._SCALETOL:
                raise IncompatibleParameterError(
                    "Difference between '{}' values is beyond allowed tolerance {}. "
                    "(handler value: {}, incompatible value: {}".format(
                        float_attr, self._SCALETOL, this_val, other_val))

        for string_attr in string_attrs_to_compare:
            this_val = getattr(self, '_' + string_attr)
            other_val = getattr(other_handler, '_' + string_attr)
            if this_val != other_val:
                raise IncompatibleParameterError(
                    "{} values are not identical. "
                    "(handler value: {}, incompatible value: {}".format(
                        string_attr, this_val, other_val))

        for unit_attr in unit_attrs_to_compare:
            this_val = getattr(self, '_' + unit_attr)
            other_val = getattr(other_handler, '_' + unit_attr)
            unit_tol = (self._SCALETOL * this_val.unit) # TODO: do we want a different quantity_tol here?
            if abs(this_val - other_val) > unit_tol:
                raise IncompatibleParameterError(
                    "Difference between '{}' values is beyond allowed tolerance {}. "
                    "(handler value: {}, incompatible value: {}".format(
                        unit_attr, unit_tol, this_val, other_val))


    def create_force(self, system, topology, **kwargs):
        existing = [system.getForce(i) for i in range(system.getNumForces())]
        existing = [
            f for f in existing if type(f) == openmm.NonbondedForce
        ]
        force = existing[0]

        # Among other sanity checks, this ensures that the switch value is 0.
        self._validate_parameters()


        # Set the nonbonded method
        settings_matched = False
        current_nb_method = force.getNonbondedMethod()


        # First, check whether the vdWHandler set the nonbonded method to LJPME, because that means
        # that electrostatics also has to be PME
        if (current_nb_method == openmm.NonbondedForce.LJPME) and (self._method != 'PME'):
            raise IncompatibleParameterError("In current Open Force Field toolkit implementation, if vdW "
                                             "treatment is set to LJPME, electrostatics must also be PME "
                                             "(electrostatics treatment currently set to {}".format(self._method))






        # Then, set nonbonded methods based on method keyword
        if self._method == 'PME':
            # Check whether the topology is nonperiodic, in which case we always switch to NoCutoff
            # (vdWHandler will have already set this to NoCutoff)
            # TODO: This is an assumption right now, and a bad one. See issue #219
            if topology.box_vectors is None:
                assert current_nb_method == openmm.NonbondedForce.NoCutoff
                settings_matched = True
                # raise IncompatibleParameterError("Electrostatics handler received PME method keyword, but a nonperiodic"
                #                                  " topology. Use of PME electrostatics requires a periodic topology.")
            else:
                if current_nb_method == openmm.NonbondedForce.LJPME:
                    pass
                    # There's no need to check for matching cutoff/tolerance here since both are hard-coded defaults
                else:
                    force.setNonbondedMethod(openmm.NonbondedForce.PME)
                    force.setCutoffDistance(9. * unit.angstrom)
                    force.setEwaldErrorTolerance(1.e-4)

            settings_matched = True

        # If vdWHandler set the nonbonded method to NoCutoff, then we don't need to change anything
        elif self._method == 'Coulomb':
            if topology.box_vectors is None:
                # (vdWHandler will have already set this to NoCutoff)
                assert current_nb_method == openmm.NonbondedForce.NoCutoff
                settings_matched = True
            else:
                raise IncompatibleParameterError("Electrostatics method set to Coulomb, and topology is periodic. "
                                                 "In the future, this will lead to use of OpenMM's CutoffPeriodic "
                                                 "Nonbonded force method, however this is not supported in the "
                                                 "current Open Force Field toolkit.")

        # If the vdWHandler set the nonbonded method to PME, then ensure that it has the same cutoff
        elif self._method == 'reaction-field':
            if topology.box_vectors is None:
                # (vdWHandler will have already set this to NoCutoff)
                assert current_nb_method == openmm.NonbondedForce.NoCutoff
                settings_matched = True
            else:
                raise IncompatibleParameterError("Electrostatics method set to reaction-field. In the future, "
                                                 "this will lead to use of OpenMM's CutoffPeriodic or CutoffNonPeriodic"
                                                " Nonbonded force method, however this is not supported in the "
                                                 "current Open Force Field toolkit")

        if not settings_matched:
            raise IncompatibleParameterError("Unable to support provided vdW method, electrostatics "
                                             "method ({}), and topology periodicity ({}) selections. Additional "
                                             "options for nonbonded treatment may be added in future versions "
                                             "of the Open Force Field toolkit.".format(self._method,
                                                                                topology.box_vectors is not None))


class ToolkitAM1BCCHandler(ParameterHandler):
    """Handle SMIRNOFF ``<ToolkitAM1BCC>`` tags

    .. warning :: This API is experimental and subject to change.
    """

    _TAGNAME = 'ToolkitAM1BCC'  # SMIRNOFF tag name to process
    _OPENMMTYPE = openmm.NonbondedForce  # OpenMM force class to create or utilize
    _DEPENDENCIES = [vdWHandler] # vdWHandler must first run NonBondedForce.addParticle for each particle in the topology
    _KWARGS = ['charge_from_molecules', 'toolkit_registry'] # Kwargs to catch when create_force is called



    def __init__(self, **kwargs):
        super().__init__(**kwargs)



    def check_handler_compatibility(self,
                                    other_handler,
                                    assume_missing_is_default=True):
        """
        Checks whether this ParameterHandler encodes compatible physics as another ParameterHandler. This is
        called if a second handler is attempted to be initialized for the same tag.

        Parameters
        ----------
        other_handler : a ParameterHandler object
            The handler to compare to.

        Raises
        ------
        IncompatibleParameterError if handler_kwargs are incompatible with existing parameters.
        """
        pass

    def assign_charge_from_molecules(self, molecule, charge_mols):
        """
        Given an input molecule, checks against a list of molecules for an isomorphic match. If found, assigns
        partial charges from the match to the input molecule.

        Parameters
        ----------
        molecule : an openforcefield.topology.FrozenMolecule
            The molecule to have partial charges assigned if a match is found.
        charge_mols : list of [openforcefield.topology.FrozenMolecule]
            A list of molecules with charges already assigned.

        Returns
        -------
        match_found : bool
            Whether a match was found. If True, the input molecule will have been modified in-place.
        """

        from networkx.algorithms.isomorphism import GraphMatcher
        import simtk.unit

        # Define the node/edge attributes that we will use to match the atoms/bonds during molecule comparison
        node_match_func = lambda x, y: ((x['atomic_number'] == y['atomic_number']) and
                                        (x['stereochemistry'] == y['stereochemistry']) and
                                        (x['is_aromatic'] == y['is_aromatic'])
                                        )
        edge_match_func = lambda x, y: ((x['bond_order'] == y['bond_order']) and
                                        (x['stereochemistry'] == y['stereochemistry']) and
                                        (x['is_aromatic'] == y['is_aromatic'])
                                        )
        # Check each charge_mol for whether it's isomorphic to the input molecule
        for charge_mol in charge_mols:
            if molecule.is_isomorphic(charge_mol):
                # Take the first valid atom indexing map
                ref_mol_G = molecule.to_networkx()
                charge_mol_G = charge_mol.to_networkx()
                GM = GraphMatcher(
                    charge_mol_G,
                    ref_mol_G,
                    node_match=node_match_func,
                    edge_match=edge_match_func)
                for mapping in GM.isomorphisms_iter():
                    topology_atom_map = mapping
                    break
                # Set the partial charges

                # Get the partial charges
                # Make a copy of the charge molecule's charges array (this way it's the right shape)
                temp_mol_charges = copy.deepcopy(simtk.unit.Quantity(charge_mol.partial_charges))
                for charge_idx, ref_idx in topology_atom_map.items():
                    temp_mol_charges[ref_idx] = charge_mol.partial_charges[charge_idx]
                molecule.partial_charges = temp_mol_charges
                return True

        # If no match was found, return False
        return False

    def create_force(self, system, topology, **kwargs):

        from openforcefield.utils.toolkits import GLOBAL_TOOLKIT_REGISTRY
        from openforcefield.topology import FrozenMolecule, TopologyAtom, TopologyVirtualSite

        existing = [system.getForce(i) for i in range(system.getNumForces())]
        existing = [f for f in existing if type(f) == self._OPENMMTYPE]
        if len(existing) == 0:
            force = self._OPENMMTYPE()
            system.addForce(force)
        else:
            force = existing[0]

        for ref_mol in topology.reference_molecules:

            # Make a temporary copy of ref_mol to assign charges from charge_mol
            temp_mol = FrozenMolecule(ref_mol)

            # First, check whether any of the reference molecules in the topology are in the charge_from_mol list
            charges_from_charge_mol = False
            if 'charge_from_molecules' in kwargs:
                charges_from_charge_mol = self.assign_charge_from_molecules(temp_mol, kwargs['charge_from_molecules'])

            # If the molecule wasn't assigned parameters from a manually-input charge_mol, calculate them here
            if not(charges_from_charge_mol):
                toolkit_registry = kwargs.get('toolkit_registry', GLOBAL_TOOLKIT_REGISTRY)
                temp_mol.generate_conformers(n_conformers=10, toolkit_registry=toolkit_registry)
                #temp_mol.compute_partial_charges(quantum_chemical_method=self._quantum_chemical_method,
                #                                 partial_charge_method=self._partial_charge_method)
                temp_mol.compute_partial_charges_am1bcc(toolkit_registry=toolkit_registry)

            # Assign charges to relevant atoms
            for topology_molecule in topology._reference_molecule_to_topology_molecules[ref_mol]:

                top_mol_particle_start_index = topology_molecule.particle_start_topology_index

                for topology_particle in topology_molecule.particles:

                    if type(topology_particle) is TopologyAtom:
                        ref_mol_particle_index = topology_particle.atom.molecule_particle_index
                        top_mol_particle_index = topology_molecule._ref_to_top_index[ref_mol_particle_index]
                    elif type(topology_particle) is TopologyVirtualSite:
                        ref_mol_particle_index = topology_particle.virtual_site.molecule_particle_index
                        top_mol_particle_index = ref_mol_particle_index
                    else:
                        raise ValueError(f'Particles of type {type(topology_particle)} are not supported')

                    topology_particle_index = top_mol_particle_start_index + top_mol_particle_index

                    particle_charge = temp_mol._partial_charges[ref_mol_particle_index]

                    # Retrieve nonbonded parameters for reference atom (charge not set yet)
                    _, sigma, epsilon = force.getParticleParameters(topology_particle_index)
                    # Set the nonbonded force with the partial charge
                    force.setParticleParameters(topology_particle_index,
                                                particle_charge, sigma,
                                                epsilon)

    # TODO: Move chargeModel and library residue charges to SMIRNOFF spec
    def postprocess_system(self, system, topology, **kwargs):

        bond_matches = self.find_matches(topology)

        # Apply bond charge increments to all appropriate force groups
        # QUESTION: Should we instead apply this to the Topology in a preprocessing step, prior to spreading out charge onto virtual sites?
        for force in system.getForces():
            if force.__class__.__name__ in [
                    'NonbondedForce'
            ]:  # TODO: We need to apply this to all Force types that involve charges, such as (Custom)GBSA forces and CustomNonbondedForce
                for (atoms, bond_match) in bond_matches.items():
                    # Get corresponding particle indices in Topology
                    bond = bond_match.parameter_type

                    particle_indices = tuple(
                        [atom.particle_index for atom in atoms])
                    # Retrieve parameters
                    [charge0, sigma0, epsilon0] = force.getParticleParameters(
                        particle_indices[0])
                    [charge1, sigma1, epsilon1] = force.getParticleParameters(
                        particle_indices[1])
                    # Apply bond charge increment
                    charge0 -= bond.increment
                    charge1 += bond.increment
                    # Update charges
                    force.setParticleParameters(particle_indices[0], charge0,
                                                sigma0, epsilon0)
                    force.setParticleParameters(particle_indices[1], charge1,
                                                sigma1, epsilon1)
                    # TODO: Calculate exceptions


class ChargeIncrementModelHandler(ParameterHandler):
    """Handle SMIRNOFF ``<ChargeIncrementModel>`` tags

    .. warning :: This API is experimental and subject to change.
    """

    class ChargeIncrementType(ParameterType):
        """A SMIRNOFF bond charge correction type.

        .. warning :: This API is experimental and subject to change.
        """
        _VALENCE_TYPE = 'Bond'  # ChemicalEnvironment valence type expected for SMARTS
        _ELEMENT_NAME = 'ChargeIncrement'

        chargeincrement = IndexedParameterAttribute(unit=unit.elementary_charge)


    _TAGNAME = 'ChargeIncrementModel'  # SMIRNOFF tag name to process
    _INFOTYPE = ChargeIncrementType  # info type to store
    _OPENMMTYPE = openmm.NonbondedForce  # OpenMM force class to create or utilize
    # TODO: The structure of this is still undecided
    _KWARGS = ['charge_from_molecules']
    _DEFAULTS = {'number_of_conformers': 10,
                 'quantum_chemical_method': 'AM1',
                 'partial_charge_method': 'CM2'}
    _ALLOWED_VALUES = {'quantum_chemical_method': ['AM1'],
                       'partial_charge_method': ['CM2']}



    def __init__(self, **kwargs):
        raise NotImplementedError("ChangeIncrementHandler is not yet implemented, pending finalization of the "
                                  "SMIRNOFF spec")
        # super().__init__(**kwargs)
        #
        # if number_of_conformers is None:
        #     self._number_of_conformers = self._DEFAULTS['number_of_conformers']
        # elif type(number_of_conformers) is str:
        #     self._number_of_conformers = int(number_of_conformers)
        # else:
        #     self._number_of_conformers = number_of_conformers
        #
        # if quantum_chemical_method is None:
        #     self._quantum_chemical_method = self._DEFAULTS['quantum_chemical_method']
        # elif number_of_conformers in self._ALLOWED_VALUES['quantum_chemical_method']:
        #     self._number_of_conformers = number_of_conformers
        #
        # if partial_charge_method is None:
        #     self._partial_charge_method = self._DEFAULTS['partial_charge_method']
        # elif partial_charge_method in self._ALLOWED_VALUES['partial_charge_method']:
        #     self._partial_charge_method = partial_charge_method



    def check_handler_compatibility(self,
                                    other_handler,
                                    assume_missing_is_default=True):
        """
        Checks whether this ParameterHandler encodes compatible physics as another ParameterHandler. This is
        called if a second handler is attempted to be initialized for the same tag.

        Parameters
        ----------
        other_handler : a ParameterHandler object
            The handler to compare to.

        Raises
        ------
        IncompatibleParameterError if handler_kwargs are incompatible with existing parameters.
        """

        int_attrs_to_compare = ['number_of_conformers']
        string_attrs_to_compare = ['quantum_chemical_method', 'partial_charge_method']

        for int_attr in int_attrs_to_compare:
            this_val = getattr(self, '_' + int_attr)
            other_val = getattr(other_handler, '_' + int_attr)
            if this_val != other_val:
                raise IncompatibleParameterError(
                    "{} values are not identical. "
                    "(handler value: {}, incompatible value: {}".format(
                        int_attr, this_val, other_val))

        for string_attr in string_attrs_to_compare:
            this_val = getattr(self, '_' + string_attr)
            other_val = getattr(other_handler, '_' + string_attr)
            if this_val != other_val:
                raise IncompatibleParameterError(
                    "{} values are not identical. "
                    "(handler value: {}, incompatible value: {}".format(
                        string_attr, this_val, other_val))


    def assign_charge_from_molecules(self, molecule, charge_mols):
        """
        Given an input molecule, checks against a list of molecules for an isomorphic match. If found, assigns
        partial charges from the match to the input molecule.

        Parameters
        ----------
        molecule : an openforcefield.topology.FrozenMolecule
            The molecule to have partial charges assigned if a match is found.
        charge_mols : list of [openforcefield.topology.FrozenMolecule]
            A list of molecules with charges already assigned.

        Returns
        -------
        match_found : bool
            Whether a match was found. If True, the input molecule will have been modified in-place.
        """

        from networkx.algorithms.isomorphism import GraphMatcher
        # Define the node/edge attributes that we will use to match the atoms/bonds during molecule comparison
        node_match_func = lambda x, y: ((x['atomic_number'] == y['atomic_number']) and
                                        (x['stereochemistry'] == y['stereochemistry']) and
                                        (x['is_aromatic'] == y['is_aromatic'])
                                        )
        edge_match_func = lambda x, y: ((x['order'] == y['order']) and
                                        (x['stereochemistry'] == y['stereochemistry']) and
                                        (x['is_aromatic'] == y['is_aromatic'])
                                        )
        # Check each charge_mol for whether it's isomorphic to the input molecule
        for charge_mol in charge_mols:
            if molecule.is_isomorphic(charge_mol):
                # Take the first valid atom indexing map
                ref_mol_G = molecule.to_networkx()
                charge_mol_G = charge_mol.to_networkX()
                GM = GraphMatcher(
                    charge_mol_G,
                    ref_mol_G,
                    node_match=node_match_func,
                    edge_match=edge_match_func)
                for mapping in GM.isomorphisms_iter():
                    topology_atom_map = mapping
                    break
                # Set the partial charges
                charge_mol_charges = charge_mol.get_partial_charges()
                temp_mol_charges = charge_mol_charges.copy()
                for charge_idx, ref_idx in topology_atom_map:
                    temp_mol_charges[ref_idx] = charge_mol_charges[charge_idx]
                molecule.set_partial_charges(temp_mol_charges)
                return True

        # If no match was found, return False
        return False

    def create_force(self, system, topology, **kwargs):


        from openforcefield.topology import FrozenMolecule, TopologyAtom, TopologyVirtualSite

        existing = [system.getForce(i) for i in range(system.getNumForces())]
        existing = [f for f in existing if type(f) == self._OPENMMTYPE]
        if len(existing) == 0:
            force = self._OPENMMTYPE()
            system.addForce(force)
        else:
            force = existing[0]

        for ref_mol in topology.reference_molecules:

            # Make a temporary copy of ref_mol to assign charges from charge_mol
            temp_mol = FrozenMolecule(ref_mol)

            # First, check whether any of the reference molecules in the topology are in the charge_from_mol list
            charges_from_charge_mol = False
            if 'charge_from_mol' in kwargs:
                charges_from_charge_mol = self.assign_charge_from_molecules(temp_mol, kwargs['charge_from_mol'])

            # If the molecule wasn't assigned parameters from a manually-input charge_mol, calculate them here
            if not(charges_from_charge_mol):
                temp_mol.generate_conformers(n_conformers=10)
                temp_mol.compute_partial_charges(quantum_chemical_method=self._quantum_chemical_method,
                                                 partial_charge_method=self._partial_charge_method)

            # Assign charges to relevant atoms
            for topology_molecule in topology._reference_molecule_to_topology_molecules[ref_mol]:
                for topology_particle in topology_molecule.particles:
                    topology_particle_index = topology_particle.topology_particle_index
                    if type(topology_particle) is TopologyAtom:
                        ref_mol_particle_index = topology_particle.atom.molecule_particle_index
                    if type(topology_particle) is TopologyVirtualSite:
                        ref_mol_particle_index = topology_particle.virtual_site.molecule_particle_index
                    particle_charge = temp_mol._partial_charges[ref_mol_particle_index]

                    # Retrieve nonbonded parameters for reference atom (charge not set yet)
                    _, sigma, epsilon = force.getParticleParameters(topology_particle_index)
                    # Set the nonbonded force with the partial charge
                    force.setParticleParameters(topology_particle_index,
                                                particle_charge, sigma,
                                                epsilon)



    # TODO: Move chargeModel and library residue charges to SMIRNOFF spec
    def postprocess_system(self, system, topology, **kwargs):
        bond_matches = self.find_matches(topology)

        # Apply bond charge increments to all appropriate force groups
        # QUESTION: Should we instead apply this to the Topology in a preprocessing step, prior to spreading out charge onto virtual sites?
        for force in system.getForces():
            if force.__class__.__name__ in [
                    'NonbondedForce'
            ]:  # TODO: We need to apply this to all Force types that involve charges, such as (Custom)GBSA forces and CustomNonbondedForce
                for (atoms, bond_match) in bond_matches.items():
                    bond = bond_match.parameter_type

                    # Get corresponding particle indices in Topology
                    particle_indices = tuple(
                        [atom.particle_index for atom in atoms])
                    # Retrieve parameters
                    [charge0, sigma0, epsilon0] = force.getParticleParameters(
                        particle_indices[0])
                    [charge1, sigma1, epsilon1] = force.getParticleParameters(
                        particle_indices[1])
                    # Apply bond charge increment
                    charge0 -= bond.increment
                    charge1 += bond.increment
                    # Update charges
                    force.setParticleParameters(particle_indices[0], charge0,
                                                sigma0, epsilon0)
                    force.setParticleParameters(particle_indices[1], charge1,
                                                sigma1, epsilon1)
                    # TODO: Calculate exceptions


class GBSAParameterHandler(ParameterHandler):
    """Handle SMIRNOFF ``<GBSAParameterHandler>`` tags

    .. warning :: This API is experimental and subject to change.
    """
    # TODO: Differentiate between global and per-particle parameters for each model.

    # Global parameters for surface area (SA) component of model
    SA_expected_parameters = {
        'ACE': ['surface_area_penalty', 'solvent_radius'],
        None: [],
    }

    # Per-particle parameters for generalized Born (GB) model
    GB_expected_parameters = {
        'HCT': ['radius', 'scale'],
        'OBC1': ['radius', 'scale'],
        'OBC2': ['radius', 'scale'],
    }

    class GBSAType(ParameterType):
        """A SMIRNOFF GBSA type.

        .. warning :: This API is experimental and subject to change.
        """
        _VALENCE_TYPE = 'Atom'
        _ELEMENT_NAME = 'Atom' # TODO: This isn't actually in the spec

        radius = ParameterAttribute(unit=unit.angstrom)
        scale = ParameterAttribute(converter=float)

        def __init__(self, **kwargs):
            super().__init__(**kwargs)

            # # Store model parameters.
            # gb_model = parent.attrib['gb_model']
            # expected_parameters = GBSAParameterHandler.GB_expected_parameters[
            #     gb_model]
            # provided_parameters = list()
            # missing_parameters = list()
            # for name in expected_parameters:
            #     if name in node.attrib:
            #         provided_parameters.append(name)
            #         value = _extract_quantity_from_xml_element(
            #             node, parent, name)
            #         setattr(self, name, value)
            #     else:
            #         missing_parameters.append(name)
            # if len(missing_parameters) > 0:
            #     msg = 'GBSAForce: missing per-atom parameters for tag %s' % str(
            #         node)
            #     msg += 'model "%s" requires specification of per-atom parameters %s\n' % (
            #         gb_model, str(expected_parameters))
            #     msg += 'provided parameters : %s\n' % str(provided_parameters)
            #     msg += 'missing parameters: %s' % str(missing_parameters)
            #     raise Exception(msg)

    # TODO: Finish this
    _TAGNAME = 'GBSA'
    _INFOTYPE = GBSAType
    #_OPENMMTYPE =

    def __init__(self, **kwargs):

        super().__init__(**kwargs)

    # TODO: Fix this
    def parseElement(self):
        # Initialize GB model
        gb_model = element.attrib['gb_model']
        valid_GB_models = GBSAParameterHandler.GB_expected_parameters.keys()
        if not gb_model in valid_GB_models:
            raise Exception(
                'Specified GBSAForce model "%s" not one of valid models: %s' %
                (gb_model, valid_GB_models))
        self.gb_model = gb_model

        # Initialize SA model
        sa_model = element.attrib['sa_model']
        valid_SA_models = GBSAParameterHandler.SA_expected_parameters.keys()
        if not sa_model in valid_SA_models:
            raise Exception(
                'Specified GBSAForce SA_model "%s" not one of valid models: %s'
                % (sa_model, valid_SA_models))
        self.sa_model = sa_model

        # Store parameters for GB and SA models
        # TODO: Deep copy?
        self.parameters = element.attrib

    # TODO: Generalize this to allow forces to know when their OpenMM Force objects can be combined
    def checkCompatibility(self, Handler):
        """
        Check compatibility of this Handler with another Handlers.
        """
        Handler = existing[0]
        if (Handler.gb_model != self.gb_model):
            raise ValueError(
                'Found multiple GBSAForce tags with different GB model specifications'
            )
        if (Handler.sa_model != self.sa_model):
            raise ValueError(
                'Found multiple GBSAForce tags with different SA model specifications'
            )
        # TODO: Check other attributes (parameters of GB and SA models) automatically?

    def create_force(self, system, topology, **args):
        # TODO: Rework this
        from openforcefield.typing.engines.smirnoff import gbsaforces
        force_class = getattr(gbsaforces, self.gb_model)
        force = force_class(**self.parameters)
        system.addForce(force)

        # Add all GBSA terms to the system.
        expected_parameters = GBSAParameterHandler.GB_expected_parameters[
            self.gb_model]

        # Create all particles with parameters set to zero
        atoms = self.getMatches(topology)
        nparams = 1 + len(expected_parameters)  # charge + GBSA parameters
        params = [0.0 for i in range(nparams)]
        for _ in topology.topology_particles():
            force.addParticle(params)
        # Set the GBSA parameters (keeping charges at zero for now)
        for (atoms, gbsa_type) in atoms.items():
            atom = atoms[0]
            # Set per-particle parameters for assigned parameters
            params = [atom.charge] + [
                getattr(gbsa_type, name) for name in expected_parameters
            ]
            force.setParticleParameters(atom.particle_index, params)


if __name__ == '__main__':
    import doctest
    doctest.run_docstring_examples(ParameterAttribute, globals())
