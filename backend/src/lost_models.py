from datetime import datetime
from sqlalchemy import String,Text,ForeignKey,CheckConstraint,Index,Boolean,DateTime,text
from sqlalchemy.orm import Mapped,mapped_column
from .db import Base
class LostItem(Base):
    __tablename__='lost_items'
    __table_args__=(CheckConstraint("state in ('stored','handed_over')"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    performance_id:Mapped[int]=mapped_column(ForeignKey('performances.id'))
    code:Mapped[str]=mapped_column(String(50),unique=True)
    location:Mapped[str]=mapped_column(String(100))
    identifying_detail:Mapped[str]=mapped_column(Text)
    state:Mapped[str]=mapped_column(String(20),default='stored')
class ItemClaim(Base):
    __tablename__='item_claims'
    __table_args__=(CheckConstraint("state in ('pending','handed_over')"),Index('one_handover_per_item','item_id',unique=True,postgresql_where=text("state = 'handed_over'"),sqlite_where=text("state = 'handed_over'")))
    id:Mapped[int]=mapped_column(primary_key=True)
    item_id:Mapped[int]=mapped_column(ForeignKey('lost_items.id'))
    claimant:Mapped[str]=mapped_column(String(100))
    proof:Mapped[str]=mapped_column(Text)
    state:Mapped[str]=mapped_column(String(20),default='pending')
    handed_by:Mapped[str|None]=mapped_column(String(100),nullable=True)
class ClaimBatch(Base):
    __tablename__='claim_batches'
    __table_args__=(CheckConstraint("state in ('composed','reserved','handed_over','cancelled','expired','rejected')"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    claimant:Mapped[str]=mapped_column(String(100))
    state:Mapped[str]=mapped_column(String(20),default='composed')
    reserved_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    expires_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    handed_by:Mapped[str|None]=mapped_column(String(100),nullable=True)
    handed_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    closed_by:Mapped[str|None]=mapped_column(String(100),nullable=True)
    closed_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    close_reason:Mapped[str]=mapped_column(String(200),default='')
class ClaimBatchItem(Base):
    __tablename__='claim_batch_items'
    __table_args__=(Index('one_active_batch_per_item','item_id',unique=True,postgresql_where=text("active"),sqlite_where=text("active")),Index('one_active_batch_per_claim','claim_id',unique=True,postgresql_where=text("active"),sqlite_where=text("active")))
    id:Mapped[int]=mapped_column(primary_key=True)
    batch_id:Mapped[int]=mapped_column(ForeignKey('claim_batches.id'))
    claim_id:Mapped[int]=mapped_column(ForeignKey('item_claims.id'))
    item_id:Mapped[int]=mapped_column(ForeignKey('lost_items.id'))
    active:Mapped[bool]=mapped_column(Boolean,default=True)
