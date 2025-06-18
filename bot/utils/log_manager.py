from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from sqlalchemy import create_engine, desc, and_
from sqlalchemy.orm import sessionmaker
from bot.config.settings import DB_URL
from bot.utils.postgres_logger import LogEntry, Base

class LogManager:
    """Utility class for managing and querying logs from PostgreSQL database."""
    
    def __init__(self):
        self.engine = create_engine(DB_URL)
        self.Session = sessionmaker(bind=self.engine)
    
    def get_recent_logs(self, hours: int = 24, level: Optional[str] = None, 
                       logger_name: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent logs with optional filtering."""
        session = self.Session()
        try:
            query = session.query(LogEntry)
            
            # Filter by time
            since = datetime.now() - timedelta(hours=hours)
            query = query.filter(LogEntry.timestamp >= since)
            
            # Filter by level if specified
            if level:
                query = query.filter(LogEntry.level == level.upper())
            
            # Filter by logger name if specified
            if logger_name:
                query = query.filter(LogEntry.logger_name == logger_name)
            
            # Order by timestamp descending and limit results
            logs = query.order_by(desc(LogEntry.timestamp)).limit(limit).all()
            
            return [
                {
                    'id': log.id,
                    'timestamp': log.timestamp.isoformat(),
                    'level': log.level,
                    'logger_name': log.logger_name,
                    'message': log.message,
                    'module': log.module,
                    'function': log.function,
                    'line_number': log.line_number,
                    'exception_info': log.exception_info,
                    'extra_data': log.extra_data
                }
                for log in logs
            ]
        finally:
            session.close()
    
    def get_errors(self, hours: int = 24, limit: int = 50) -> List[Dict[str, Any]]:
        """Get error-level logs from the last N hours."""
        return self.get_recent_logs(hours=hours, level='ERROR', limit=limit)
    
    def get_warnings(self, hours: int = 24, limit: int = 50) -> List[Dict[str, Any]]:
        """Get warning-level logs from the last N hours."""
        return self.get_recent_logs(hours=hours, level='WARNING', limit=limit)
    
    def search_logs(self, search_term: str, hours: int = 24, limit: int = 50) -> List[Dict[str, Any]]:
        """Search logs by message content."""
        session = self.Session()
        try:
            since = datetime.now() - timedelta(hours=hours)
            logs = session.query(LogEntry).filter(
                and_(
                    LogEntry.timestamp >= since,
                    LogEntry.message.ilike(f'%{search_term}%')
                )
            ).order_by(desc(LogEntry.timestamp)).limit(limit).all()
            
            return [
                {
                    'id': log.id,
                    'timestamp': log.timestamp.isoformat(),
                    'level': log.level,
                    'logger_name': log.logger_name,
                    'message': log.message,
                    'module': log.module,
                    'function': log.function,
                    'line_number': log.line_number,
                    'exception_info': log.exception_info,
                    'extra_data': log.extra_data
                }
                for log in logs
            ]
        finally:
            session.close()
    
    def get_log_statistics(self, hours: int = 24) -> Dict[str, Any]:
        """Get statistics about logs in the last N hours."""
        session = self.Session()
        try:
            since = datetime.now() - timedelta(hours=hours)
            
            # Total logs
            total_logs = session.query(LogEntry).filter(LogEntry.timestamp >= since).count()
            
            # Logs by level
            from sqlalchemy import func
            level_counts = session.query(
                LogEntry.level, 
                func.count(LogEntry.id).label('count')
            ).filter(LogEntry.timestamp >= since).group_by(LogEntry.level).all()
            
            # Logs by logger
            logger_counts = session.query(
                LogEntry.logger_name, 
                func.count(LogEntry.id).label('count')
            ).filter(LogEntry.timestamp >= since).group_by(LogEntry.logger_name).order_by(
                func.count(LogEntry.id).desc()
            ).limit(10).all()
            
            return {
                'total_logs': total_logs,
                'level_distribution': {level: count for level, count in level_counts},
                'top_loggers': {logger: count for logger, count in logger_counts},
                'time_period_hours': hours
            }
        finally:
            session.close()
    
    def cleanup_old_logs(self, days: int = 30) -> int:
        """Delete logs older than specified days. Returns number of deleted records."""
        session = self.Session()
        try:
            cutoff_date = datetime.now() - timedelta(days=days)
            deleted_count = session.query(LogEntry).filter(LogEntry.timestamp < cutoff_date).delete()
            session.commit()
            return deleted_count
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
    
    def close(self):
        """Close the database connection."""
        if hasattr(self, 'engine'):
            self.engine.dispose()

# Convenience functions for quick access
def get_recent_errors(hours: int = 24, limit: int = 50) -> List[Dict[str, Any]]:
    """Quick function to get recent errors."""
    manager = LogManager()
    try:
        return manager.get_errors(hours=hours, limit=limit)
    finally:
        manager.close()

def get_log_stats(hours: int = 24) -> Dict[str, Any]:
    """Quick function to get log statistics."""
    manager = LogManager()
    try:
        return manager.get_log_statistics(hours=hours)
    finally:
        manager.close()

def cleanup_logs(days: int = 30) -> int:
    """Quick function to cleanup old logs."""
    manager = LogManager()
    try:
        return manager.cleanup_old_logs(days=days)
    finally:
        manager.close() 