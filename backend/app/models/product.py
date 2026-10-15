"""Product Model"""
from sqlalchemy import Column, String, Text, Float, Integer, JSON
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class Product(Base):
    """Product database model"""
    __tablename__ = "products"
    
    id = Column(String(20), primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)
    category = Column(String(50), index=True)
    description = Column(Text)
    specifications = Column(JSON, default=dict)
    stock = Column(Integer, default=0)
    keywords = Column(JSON, default=list)
    created_at = Column(UTCDateTime, default=utcnow)
    updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow)
    
    @property
    def is_out_of_stock(self) -> bool:
        """Check if product is out of stock"""
        return self.stock == 0
    
    def __repr__(self):
        return f"<Product(id='{self.id}', name='{self.name}', stock={self.stock})>"
