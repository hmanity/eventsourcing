from collections import namedtuple
from uuid import UUID

from sqlalchemy import DECIMAL
from sqlalchemy.sql.schema import Column, Index
from sqlalchemy.sql.sqltypes import BigInteger, Integer, String, Text
from sqlalchemy_utils.types.uuid import UUIDType

from eventsourcing.application.policies import PersistencePolicy
from eventsourcing.example.domainmodel import create_new_example
from eventsourcing.example.infrastructure import ExampleRepository
from eventsourcing.infrastructure.eventstore import EventStore
from eventsourcing.infrastructure.sequenceditemmapper import SequencedItemMapper
from eventsourcing.infrastructure.sqlalchemy.activerecords import SQLAlchemyActiveRecordStrategy
from eventsourcing.infrastructure.sqlalchemy.datastore import ActiveRecord, SQLAlchemyDatastore, SQLAlchemySettings
from eventsourcing.tests.datastore_tests.base import AbstractDatastoreTestCase

# This module explores extending the sequenced item class with some more fields. How easy is it?
# Just needed to define the extended type, define a suitable active record
# class, and extend the sequenced itemevent mapper to derive values for the
# extra attributes. It's easy.

# Define the sequenced item class.
ExtendedSequencedItem = namedtuple('ExtendedSequencedItem',
                                   ['sequence_id', 'position', 'topic', 'data', 'timestamp', 'event_type'])


# Extend the database table definition to support the extra fields.
class ExtendedIntegerSequencedItemRecord(ActiveRecord):
    __tablename__ = 'extended_integer_sequenced_items'

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)

    # Sequence ID (e.g. an entity or aggregate ID).
    sequence_id = Column(UUIDType(), nullable=False)

    # Position (index) of item in sequence.
    position = Column(BigInteger(), nullable=False)

    # Topic of the item (e.g. path to domain event class).
    topic = Column(String(255), nullable=False)

    # State of the item (serialized dict, possibly encrypted).
    data = Column(Text())

    # Timestamp of the event.
    timestamp = Column(DECIMAL(24, 6, 6), nullable=False)
    # timestamp = Column(DECIMAL(27, 9, 9), nullable=False)

    # Type of the event (class name).
    event_type = Column(String(255))

    __table_args__ = (
        Index('integer_sequenced_items_index', 'sequence_id', 'position', unique=True),
    )


# Extend the sequenced item mapper to derive the extra values.
class ExtendedSequencedItemMapper(SequencedItemMapper):
    def construct_item_args(self, domain_event):
        args = super(ExtendedSequencedItemMapper, self).construct_item_args(domain_event)
        event_type = domain_event.__class__.__qualname__
        return args + (event_type,)


# Define an application object.
class ExampleApplicationWithExtendedSequencedItemType(object):
    def __init__(self, session):
        self.event_store = EventStore(
            active_record_strategy=SQLAlchemyActiveRecordStrategy(
                session=session,
                active_record_class=ExtendedIntegerSequencedItemRecord,
                sequenced_item_class=ExtendedSequencedItem,
            ),
            sequenced_item_mapper=ExtendedSequencedItemMapper(
                sequenced_item_class=ExtendedSequencedItem,
                sequence_id_attr_name='originator_id',
                position_attr_name='originator_version',
                other_attr_names=('timestamp',),
            )
        )
        self.repository = ExampleRepository(
            event_store=self.event_store,
        )
        self.persistence_policy = PersistencePolicy(self.event_store)

    def close(self):
        self.persistence_policy.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class TestExampleWithExtendedSequencedItemType(AbstractDatastoreTestCase):
    def setUp(self):
        super(TestExampleWithExtendedSequencedItemType, self).setUp()
        self.datastore.setup_connection()
        self.datastore.setup_tables()

    def tearDown(self):
        self.datastore.drop_tables()
        self.datastore.close_connection()
        super(TestExampleWithExtendedSequencedItemType, self).setUp()

    def construct_datastore(self):
        return SQLAlchemyDatastore(
            base=ActiveRecord,
            settings=SQLAlchemySettings(),
            tables=(ExtendedIntegerSequencedItemRecord,)
        )

    def test(self):
        with ExampleApplicationWithExtendedSequencedItemType(self.datastore.session) as app:
            # Create entity.
            entity1 = create_new_example(a='a', b='b')
            self.assertIsInstance(entity1.id, UUID)
            self.assertEqual(entity1.a, 'a')
            self.assertEqual(entity1.b, 'b')

            # Check there is a stored event.
            all_records = list(app.event_store.active_record_strategy.all_records())
            self.assertEqual(len(all_records), 1)
            active_record = all_records[0]
            self.assertEqual(active_record.sequence_id, entity1.id)
            self.assertEqual(active_record.position, 0)
            self.assertEqual(active_record.event_type, 'Example.Created', active_record.event_type)
            self.assertEqual(active_record.timestamp, entity1.__created_on__)

            # Read entity from repo.
            retrieved_obj = app.repository[entity1.id]
            self.assertEqual(retrieved_obj.id, entity1.id)
