import logging
import json
from datetime import datetime
from sqlalchemy import create_engine, Column, String, DateTime, Text, Integer
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from bot.config.settings import DB_URL

Base = declarative_base()

class LogEntry(Base):
    __tablename__ = 'log_entries'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.now, nullable=False)
    level = Column(String(10), nullable=False)
    logger_name = Column(String(100), nullable=False)
    message = Column(Text, nullable=False)
    module = Column(String(100), nullable=True)
    function = Column(String(100), nullable=True)
    line_number = Column(Integer, nullable=True)
    exception_info = Column(Text, nullable=True)
    extra_data = Column(Text, nullable=True)  # JSON field for additional data

class PostgreSQLHandler(logging.Handler):
    """Custom logging handler that stores logs in PostgreSQL database."""
    
    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
        self.engine = create_engine(DB_URL)
        self.Session = sessionmaker(bind=self.engine)
        
        # Create the log_entries table if it doesn't exist
        Base.metadata.create_all(self.engine)
    
    def emit(self, record):
        """Emit a record to the database."""
        try:
            session = self.Session()
            
            # Prepare extra data as JSON
            extra_data = {}
            if hasattr(record, 'extra_data'):
                extra_data = record.extra_data
            
            # Get exception info if present
            exception_info = None
            if record.exc_info:
                exception_info = self.formatException(record.exc_info)
            
            # Create log entry
            log_entry = LogEntry(
                timestamp=datetime.fromtimestamp(record.created),
                level=record.levelname,
                logger_name=record.name,
                message=record.getMessage(),
                module=record.module,
                function=record.funcName,
                line_number=record.lineno,
                exception_info=exception_info,
                extra_data=json.dumps(extra_data) if extra_data else None
            )
            
            session.add(log_entry)
            session.commit()
            
        except Exception as e:
            # Fallback to console if database logging fails
            print(f"Failed to log to database: {e}")
            print(f"Original log message: {record.getMessage()}")
        finally:
            if 'session' in locals():
                session.close()
    
    def close(self):
        """Close the handler and clean up resources."""
        if hasattr(self, 'engine'):
            self.engine.dispose()
        super().close() 