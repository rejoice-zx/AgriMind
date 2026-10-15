"""Weather Snapshot Model - 田块微气象与历史环境数据模型"""
from sqlalchemy import Column, Integer, String, Float, ForeignKey, Index
from sqlalchemy.orm import relationship
from app.core.model_base import Base
from app.utils.time import utcnow, UTCDateTime


class WeatherSnapshot(Base):
    """田块天气与环境时序快照模型"""
    __tablename__ = "weather_snapshots"
    __table_args__ = (
        Index("ix_weather_snapshots_field_timestamp", "field_id", "timestamp"),
    )

    id = Column(Integer, primary_key=True, index=True)
    field_id = Column(Integer, ForeignKey("fields.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp = Column(UTCDateTime, default=utcnow, index=True)  # 采集/预报时间点
    temperature = Column(Float, nullable=True)  # 温度（℃）
    humidity = Column(Float, nullable=True)  # 相对湿度（%）
    rainfall = Column(Float, default=0.0)  # 降雨量（mm）
    wind_speed = Column(Float, nullable=True)  # 风速（m/s）
    weather = Column(String(50), nullable=True)  # 天气状况（晴、多云、小雨等）
    source = Column(String(50), default="api")  # api, iot_sensor, manual
    created_at = Column(UTCDateTime, default=utcnow)

    # 关联关系
    field = relationship("Field", back_populates="weather_snapshots")

    def __repr__(self):
        return f"<WeatherSnapshot(id={self.id}, field_id={self.field_id}, temp={self.temperature}, hum={self.humidity})>"
