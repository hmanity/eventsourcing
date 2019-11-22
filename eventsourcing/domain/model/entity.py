from decimal import Decimal
from typing import Any, Dict, Optional, Type, TypeVar
from uuid import UUID, uuid4

import eventsourcing.domain.model.events as events
from eventsourcing.domain.model.decorators import subclassevents
from eventsourcing.domain.model.events import (
    DomainEvent,
    EventWithHash,
    EventWithOriginatorID,
    EventWithOriginatorVersion,
    EventWithTimestamp,
    GENESIS_HASH,
    publish,
)
from eventsourcing.exceptions import (
    EntityIsDiscarded,
    HeadHashError,
    OriginatorIDError,
    OriginatorVersionError,
)
from eventsourcing.types import (
    AbstractDomainEntity,
    MetaAbstractDomainEntity,
    T_ev_evs,
    T_aev,
)
from eventsourcing.utils.times import decimaltimestamp_from_uuid
from eventsourcing.utils.topic import get_topic, resolve_topic


class MetaDomainEntity(MetaAbstractDomainEntity):
    __subclassevents__ = False

    # Todo: Delete the '**kwargs' when library no longer supports Python3.6.
    #  - When we started using typing.Generic, we started getting
    #    an error in 3.6 (only) "unexpected keyword argument 'tvars'"
    #    which was cured by adding **kwargs here. It's not needed
    #    for Python3.7, and only supports backward compatibility.
    #    So it can be removed when support for Python 3.6 dropped.
    def __init__(cls, name: str, *args: Any, **kwargs: Any) -> None:
        super().__init__(name, *args, **kwargs)
        if name == "_gorg":
            # Todo: Also Remove this block when dropping support for Python 3.6.
            # Needed in 3.6 only, stops infinite recursion between typing and abc
            # doing subclass checks. Don't know why. Seems issue fixed in Python 3.7.
            pass
        elif cls.__subclassevents__ is True:
            # Redefine entity domain events.
            subclassevents(cls)


T_en = TypeVar("T_en", bound="DomainEntity")
T_ev = TypeVar("T_ev", bound="DomainEntity.Event")
T_ev_created = TypeVar("T_ev_created", bound="DomainEntity.Created")


class DomainEntity(AbstractDomainEntity[T_aev], metaclass=MetaDomainEntity):
    """
    Supertype for domain model entity.
    """

    __subclassevents__ = False

    class Event(EventWithOriginatorID[T_en], DomainEvent[T_en]):
        """
        Supertype for events of domain model entities.
        """

        def __check_obj__(self, obj: T_en) -> None:
            """
            Checks state of obj before mutating.

            :param obj: Domain entity to be checked.

            :raises OriginatorIDError: if the originator_id is mismatched
            """
            assert isinstance(obj, DomainEntity)  # For PyCharm navigation.
            # Assert ID matches originator ID.
            if obj.id != self.originator_id:
                raise OriginatorIDError(
                    "'{}' not equal to event originator ID '{}'"
                    "".format(obj.id, self.originator_id)
                )

    @classmethod
    def __create__(
        cls: Type[T_en],
        originator_id: Optional[UUID] = None,
        event_class: Optional[Type["DomainEntity.Created[T_en]"]] = None,
        **kwargs: Any,
    ) -> T_en:
        """
        Creates a new domain entity.

        Constructs a "created" event, constructs the entity object
        from the event, publishes the "created" event, and returns
        the new domain entity object.

        :param cls DomainEntity: Class of domain event
        :param originator_id: ID of the new domain entity (defaults to ``uuid4()``).
        :param event_class: Domain event class to be used for the "created" event.
        :param kwargs: Other named attribute values of the "created" event.
        :return: New domain entity object.
        :rtype: DomainEntity
        """

        if originator_id is None:
            originator_id = uuid4()

        if event_class:
            created_event_class: Type[DomainEntity.Created[T_en]] = event_class
        else:
            assert issubclass(cls, DomainEntity)  # For navigation in PyCharm.
            created_event_class = cls.Created

        event = created_event_class(
            originator_id=originator_id, originator_topic=get_topic(cls), **kwargs
        )

        obj = event.__mutate__(None)

        assert obj is not None, "{} returned None".format(
            type(event).__mutate__.__qualname__
        )

        obj.__publish__(event)
        return obj

    class Created(events.Created[T_en], Event[T_en]):
        """
        Triggered when an entity is created.
        """

        def __init__(self, originator_topic: str, **kwargs: Any):
            super(DomainEntity.Created, self).__init__(
                originator_topic=originator_topic, **kwargs
            )

        @property
        def originator_topic(self) -> str:
            """
            Topic (a string) representing the class of the originating domain entity.

            :rtype: str
            """
            return self.__dict__["originator_topic"]

        def __mutate__(self, obj: Optional[T_en]) -> Optional[T_en]:
            """
            Constructs object from an entity class,
            which is obtained by resolving the originator topic,
            unless it is given as method argument ``entity_class``.

            :param entity_class: Class of domain entity to be constructed.
            """
            entity_class: Type[T_en] = resolve_topic(self.originator_topic)
            return entity_class(**self.__entity_kwargs__)

        @property
        def __entity_kwargs__(self) -> Dict[str, Any]:
            kwargs = self.__dict__.copy()
            kwargs["id"] = kwargs.pop("originator_id")
            kwargs.pop("originator_topic", None)
            kwargs.pop("__event_topic__", None)
            return kwargs

    def __init__(self, id: UUID):
        self._id = id
        self.__is_discarded__ = False

    @property
    def id(self) -> UUID:
        """The immutable ID of the domain entity.

        This value is set using the ``originator_id`` of the
        "created" event constructed by ``__create__()``.

        An entity ID allows an instance to be
        referenced and distinguished from others, even
        though its state may change over time.

        This attribute has the normal "public" format for a Python object
        attribute name, because by definition all domain entities have an ID.
        """
        return self._id

    def __change_attribute__(self: T_en, name: str, value: Any) -> None:
        """
        Changes named attribute with the given value,
        by triggering an AttributeChanged event.
        """
        event_class: Type["DomainEntity.AttributeChanged[T_en]"] = self.AttributeChanged
        assert isinstance(self, DomainEntity)  # For PyCharm navigation.
        self.__trigger_event__(event_class=event_class, name=name, value=value)

    class AttributeChanged(Event[T_en], events.AttributeChanged[T_en]):
        """
        Triggered when a named attribute is assigned a new value.
        """

        def __mutate__(self, obj: Optional[T_en]) -> Optional[T_en]:
            obj = super(DomainEntity.AttributeChanged, self).__mutate__(obj)
            setattr(obj, self.name, self.value)
            return obj

    def __discard__(self: T_en) -> None:
        """
        Discards self, by triggering a Discarded event.
        """
        event_class: Type["DomainEntity.Discarded[T_en]"] = self.Discarded
        assert isinstance(self, DomainEntity)  # For PyCharm navigation.
        self.__trigger_event__(event_class=event_class)

    class Discarded(events.Discarded[T_en], Event[T_en]):
        """
        Triggered when a DomainEntity is discarded.
        """

        def __mutate__(self, obj: Optional[T_en]) -> Optional[T_en]:
            obj = super(DomainEntity.Discarded, self).__mutate__(obj)
            if obj is not None:
                assert isinstance(obj, DomainEntity)  # For PyCharm navigation.
                obj.__is_discarded__ = True
            return None

    def __assert_not_discarded__(self) -> None:
        """
        Asserts that this entity has not been discarded.

        Raises EntityIsDiscarded exception if entity has been discarded already.
        """
        if self.__is_discarded__:
            raise EntityIsDiscarded("Entity is discarded")

    def __trigger_event__(self, event_class: Type[T_aev], **kwargs: Any) -> None:
        """
        Constructs, applies, and publishes a domain event.
        """
        self.__assert_not_discarded__()
        event: T_aev = event_class(originator_id=self.id, **kwargs)
        self.__mutate__(event)
        self.__publish__(event)

    def __mutate__(self, event: T_aev) -> None:
        """
        Mutates this entity with the given event.

        This method calls on the event object to mutate this
        entity, because the mutation behaviour of different types
        of events was usefully factored onto the event classes, and
        the event mutate() method is the most convenient way to
        defined behaviour in domain models.

        However, as an alternative to implementing the mutate()
        method on domain model events, this method can be extended
        with a method that is capable of mutating an entity for all
        the domain event classes introduced by the entity class.

        Similarly, this method can be overridden entirely in subclasses,
        so long as all of the mutation behaviour is implemented in the
        mutator function, including the mutation behaviour of the events
        defined on the library event classes that would no longer be invoked.

        However, if the entity class defines a mutator function, or if a
        separate mutator function is used, then it must be involved in
        the event sourced repository used to replay events, which by default
        knows nothing about the domain entity class. In practice, this
        means having a repository for each kind of entity, rather than
        the application just having one repository, with each repository
        having a mutator function that can project the entity events
        into an entity.
        """
        assert isinstance(event, DomainEntity.Event)
        event.__mutate__(self)

    def __publish__(self, event: T_ev_evs) -> None:
        """
        Publishes given event for subscribers in the application.

        :param event: domain event or list of events
        """
        self.__publish_to_subscribers__(event)

    def __publish_to_subscribers__(self, event: T_ev_evs) -> None:
        """
        Actually dispatches given event to publish-subscribe mechanism.

        :param event: domain event or list of events
        """
        publish(event)

    def __eq__(self, other: object) -> bool:
        return type(self) == type(other) and self.__dict__ == other.__dict__

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)


T_en_hashchain = TypeVar("T_en_hashchain", bound="EntityWithHashchain")


class EntityWithHashchain(DomainEntity):
    __genesis_hash__ = GENESIS_HASH

    def __init__(self, *args: Any, **kwargs: Any):
        super(EntityWithHashchain, self).__init__(*args, **kwargs)
        self.__head__: str = type(self).__genesis_hash__

    class Event(EventWithHash[T_en_hashchain], DomainEntity.Event[T_en_hashchain]):
        """
        Supertype for events of domain entities.
        """

        def __mutate__(self, obj: Optional[T_en_hashchain]) -> Optional[T_en_hashchain]:
            # Call super method.
            obj = super(EntityWithHashchain.Event, self).__mutate__(obj)

            # Set entity head from event hash.
            #  - unless just discarded...
            if obj is not None:
                assert isinstance(obj, EntityWithHashchain)
                obj.__head__ = self.__event_hash__

            return obj

        def __check_obj__(self, obj: T_en_hashchain) -> None:
            """
            Extends superclass method by checking the __previous_hash__
            of this event matches the __head__ hash of the entity obj.
            """
            # Call super method.
            super(EntityWithHashchain.Event, self).__check_obj__(obj)
            assert isinstance(obj, EntityWithHashchain)  # For PyCharm navigation.
            # Assert __head__ matches previous hash.
            if obj.__head__ != self.__dict__.get("__previous_hash__"):
                raise HeadHashError(obj.id, obj.__head__, type(self))

    class Created(Event[T_en_hashchain], DomainEntity.Created[T_en_hashchain]):
        @property
        def __entity_kwargs__(self) -> Dict[str, Any]:
            # Get super property.
            kwargs = super(EntityWithHashchain.Created, self).__entity_kwargs__

            # Drop the event hashes.
            kwargs.pop("__event_hash__", None)
            kwargs.pop("__previous_hash__", None)

            return kwargs

    class AttributeChanged(
        Event[T_en_hashchain], DomainEntity.AttributeChanged[T_en_hashchain]
    ):
        pass

    class Discarded(Event[T_en_hashchain], DomainEntity.Discarded[T_en_hashchain]):
        def __mutate__(self, obj: Optional[T_en_hashchain]) -> Optional[T_en_hashchain]:
            # Set entity head from event hash.
            if obj:
                assert isinstance(obj, EntityWithHashchain)  # For PyCharm navigation.
                obj.__head__ = self.__event_hash__

            # Call super method.
            return super(EntityWithHashchain.Discarded, self).__mutate__(obj)

    @classmethod
    def __create__(
        cls: Type[T_en_hashchain], *args: Any, **kwargs: Any
    ) -> T_en_hashchain:
        assert issubclass(cls, EntityWithHashchain)
        # Initialise the hash-chain with "genesis hash".
        kwargs["__previous_hash__"] = getattr(cls, "__genesis_hash__", GENESIS_HASH)
        obj = super(EntityWithHashchain, cls).__create__(*args, **kwargs)
        assert isinstance(obj, EntityWithHashchain)  # For PyCharm type checking.
        return obj

    def __trigger_event__(self, event_class: Type[T_aev], **kwargs: Any) -> None:
        kwargs["__previous_hash__"] = self.__head__
        super(EntityWithHashchain, self).__trigger_event__(event_class, **kwargs)


T_en_ver = TypeVar("T_en_ver", bound="VersionedEntity")


class VersionedEntity(DomainEntity):
    def __init__(self, __version__: int, **kwargs: Any):
        super().__init__(**kwargs)
        self.___version__: int = __version__

    @property
    def __version__(self) -> int:
        return self.___version__

    def __trigger_event__(self, event_class: Type[T_aev], **kwargs: Any) -> None:
        """
        Increments the version number when an event is triggered.

        The event carries the version number that the originator
        will have when the originator is mutated with this event.
        (The event's "originator" version isn't the version of the
        originator before the event was triggered, but represents
        the result of the work of incrementing the version, which
        is then set in the event as normal. The Created event has
        version 0, and a newly created instance is at version 0.
        The second event has originator version 1, and so will the
        originator when the second event has been applied.
        """
        # Do the work of incrementing the version number.
        next_version = self.__version__ + 1
        # Trigger an event with the result of this work.
        super(VersionedEntity, self).__trigger_event__(
            event_class=event_class, originator_version=next_version, **kwargs
        )

    class Event(EventWithOriginatorVersion[T_en_ver], DomainEntity.Event[T_en_ver]):
        """Supertype for events of versioned entities."""

        def __mutate__(self, obj: Optional[T_en_ver]) -> Optional[T_en_ver]:
            obj = super(VersionedEntity.Event, self).__mutate__(obj)
            if obj is not None:
                assert isinstance(obj, VersionedEntity)  # For PyCharm navigation.
                obj.___version__ = self.originator_version
            return obj

        def __check_obj__(self, obj: T_en_ver) -> None:
            """
            Extends superclass method by checking the event's
            originator version follows (1 +) this entity's version.
            """
            super(VersionedEntity.Event, self).__check_obj__(obj)
            assert isinstance(obj, VersionedEntity)  # For PyCharm navigation.
            # Assert the version sequence is correct.
            if self.originator_version != obj.__version__ + 1:
                raise OriginatorVersionError(
                    (
                        "Event takes entity to version {}, "
                        "but entity is currently at version {}. "
                        "Event type: '{}', entity type: '{}', entity ID: '{}'"
                        "".format(
                            self.originator_version,
                            obj.__version__,
                            type(self).__name__,
                            type(obj).__name__,
                            obj._id,
                        )
                    )
                )

    class Created(DomainEntity.Created[T_en_ver], Event[T_en_ver]):
        """Published when a VersionedEntity is created."""

        def __init__(self, originator_version: int = 0, *args: Any, **kwargs: Any):
            super(VersionedEntity.Created, self).__init__(
                originator_version=originator_version, *args, **kwargs
            )

        @property
        def __entity_kwargs__(self) -> Dict[str, Any]:
            # Get super property.
            kwargs = super(VersionedEntity.Created, self).__entity_kwargs__
            kwargs["__version__"] = kwargs.pop("originator_version")
            return kwargs

    class AttributeChanged(Event[T_en_ver], DomainEntity.AttributeChanged[T_en_ver]):
        """Published when a VersionedEntity is changed."""

    class Discarded(Event[T_en_ver], DomainEntity.Discarded[T_en_ver]):
        """Published when a VersionedEntity is discarded."""


T_en_tim = TypeVar("T_en_tim", bound="TimestampedEntity")


class TimestampedEntity(DomainEntity):
    def __init__(self, __created_on__: Decimal, **kwargs: Any):
        super(TimestampedEntity, self).__init__(**kwargs)
        self.___created_on__ = __created_on__
        self.___last_modified__ = __created_on__

    @property
    def __created_on__(self) -> Decimal:
        return self.___created_on__

    @property
    def __last_modified__(self) -> Decimal:
        return self.___last_modified__

    class Event(DomainEntity.Event[T_en_tim], EventWithTimestamp[T_en_tim]):
        """Supertype for events of timestamped entities."""

        def __mutate__(self, obj: Optional[T_en_tim]) -> Optional[T_en_tim]:
            """Updates 'obj' with values from self."""
            obj = super(TimestampedEntity.Event, self).__mutate__(obj)
            if obj is not None:
                assert isinstance(obj, TimestampedEntity)  # For PyCharm navigation.
                obj.___last_modified__ = self.timestamp
            return obj

    class Created(DomainEntity.Created[T_en_tim], Event[T_en_tim]):
        """Published when a TimestampedEntity is created."""

        @property
        def __entity_kwargs__(self) -> Dict[str, Any]:
            # Get super property.
            kwargs = super(TimestampedEntity.Created, self).__entity_kwargs__
            kwargs["__created_on__"] = kwargs.pop("timestamp")
            return kwargs

    class AttributeChanged(Event[T_en_tim], DomainEntity.AttributeChanged[T_en_tim]):
        """Published when a TimestampedEntity is changed."""

    class Discarded(Event[T_en_tim], DomainEntity.Discarded[T_en_tim]):
        """Published when a TimestampedEntity is discarded."""


# Todo: Move stuff from "test_customise_with_alternative_domain_event_type" in here (
#  to define event classes
#  and update ___last_event_id__ in mutate method).


class TimeuuidedEntity(DomainEntity):
    def __init__(self, event_id: UUID, **kwargs: Any) -> None:
        super(TimeuuidedEntity, self).__init__(**kwargs)
        self.___initial_event_id__ = event_id
        self.___last_event_id__ = event_id

    @property
    def __created_on__(self) -> Decimal:
        return decimaltimestamp_from_uuid(self.___initial_event_id__)

    @property
    def __last_modified__(self) -> Decimal:
        return decimaltimestamp_from_uuid(self.___last_event_id__)


T_en_tim_ver = TypeVar("T_en_tim_ver", bound="TimestampedVersionedEntity")


class TimestampedVersionedEntity(TimestampedEntity, VersionedEntity):
    class Event(
        TimestampedEntity.Event[T_en_tim_ver], VersionedEntity.Event[T_en_tim_ver]
    ):
        """Supertype for events of timestamped, versioned entities."""

    class Created(
        TimestampedEntity.Created[T_en_tim_ver],
        VersionedEntity.Created,
        Event[T_en_tim_ver],
    ):
        """Published when a TimestampedVersionedEntity is created."""

    class AttributeChanged(
        Event[T_en_tim_ver],
        TimestampedEntity.AttributeChanged[T_en_tim_ver],
        VersionedEntity.AttributeChanged[T_en_tim_ver],
    ):
        """Published when a TimestampedVersionedEntity is created."""

    class Discarded(
        Event[T_en_tim_ver],
        TimestampedEntity.Discarded[T_en_tim_ver],
        VersionedEntity.Discarded[T_en_tim_ver],
    ):
        """Published when a TimestampedVersionedEntity is discarded."""


class TimeuuidedVersionedEntity(TimeuuidedEntity, VersionedEntity):
    pass
