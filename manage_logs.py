#!/usr/bin/env python3
"""
CLI tool for managing and querying logs stored in PostgreSQL database.
"""

import argparse
import json
from datetime import datetime, timedelta
from bot.utils.log_manager import LogManager

def format_log_entry(log_entry):
    """Format a log entry for display."""
    timestamp = datetime.fromisoformat(log_entry['timestamp'])
    return f"[{timestamp.strftime('%Y-%m-%d %H:%M:%S')}] {log_entry['level']:8} {log_entry['logger_name']:20} - {log_entry['message']}"

def main():
    parser = argparse.ArgumentParser(description='Manage and query logs from PostgreSQL database')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Recent logs command
    recent_parser = subparsers.add_parser('recent', help='Get recent logs')
    recent_parser.add_argument('--hours', type=int, default=24, help='Hours to look back (default: 24)')
    recent_parser.add_argument('--level', type=str, help='Filter by log level (DEBUG, INFO, WARNING, ERROR)')
    recent_parser.add_argument('--logger', type=str, help='Filter by logger name')
    recent_parser.add_argument('--limit', type=int, default=50, help='Maximum number of logs to return (default: 50)')
    recent_parser.add_argument('--json', action='store_true', help='Output in JSON format')
    
    # Errors command
    errors_parser = subparsers.add_parser('errors', help='Get recent errors')
    errors_parser.add_argument('--hours', type=int, default=24, help='Hours to look back (default: 24)')
    errors_parser.add_argument('--limit', type=int, default=50, help='Maximum number of errors to return (default: 50)')
    errors_parser.add_argument('--json', action='store_true', help='Output in JSON format')
    
    # Warnings command
    warnings_parser = subparsers.add_parser('warnings', help='Get recent warnings')
    warnings_parser.add_argument('--hours', type=int, default=24, help='Hours to look back (default: 24)')
    warnings_parser.add_argument('--limit', type=int, default=50, help='Maximum number of warnings to return (default: 50)')
    warnings_parser.add_argument('--json', action='store_true', help='Output in JSON format')
    
    # Search command
    search_parser = subparsers.add_parser('search', help='Search logs by message content')
    search_parser.add_argument('term', type=str, help='Search term')
    search_parser.add_argument('--hours', type=int, default=24, help='Hours to look back (default: 24)')
    search_parser.add_argument('--limit', type=int, default=50, help='Maximum number of results to return (default: 50)')
    search_parser.add_argument('--json', action='store_true', help='Output in JSON format')
    
    # Stats command
    stats_parser = subparsers.add_parser('stats', help='Get log statistics')
    stats_parser.add_argument('--hours', type=int, default=24, help='Hours to look back (default: 24)')
    stats_parser.add_argument('--json', action='store_true', help='Output in JSON format')
    
    # Cleanup command
    cleanup_parser = subparsers.add_parser('cleanup', help='Clean up old logs')
    cleanup_parser.add_argument('--days', type=int, default=30, help='Delete logs older than N days (default: 30)')
    cleanup_parser.add_argument('--dry-run', action='store_true', help='Show what would be deleted without actually deleting')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    manager = LogManager()
    
    try:
        if args.command == 'recent':
            logs = manager.get_recent_logs(
                hours=args.hours,
                level=args.level,
                logger_name=args.logger,
                limit=args.limit
            )
            
            if args.json:
                print(json.dumps(logs, indent=2))
            else:
                for log in logs:
                    print(format_log_entry(log))
        
        elif args.command == 'errors':
            logs = manager.get_errors(hours=args.hours, limit=args.limit)
            
            if args.json:
                print(json.dumps(logs, indent=2))
            else:
                for log in logs:
                    print(format_log_entry(log))
        
        elif args.command == 'warnings':
            logs = manager.get_warnings(hours=args.hours, limit=args.limit)
            
            if args.json:
                print(json.dumps(logs, indent=2))
            else:
                for log in logs:
                    print(format_log_entry(log))
        
        elif args.command == 'search':
            logs = manager.search_logs(args.term, hours=args.hours, limit=args.limit)
            
            if args.json:
                print(json.dumps(logs, indent=2))
            else:
                for log in logs:
                    print(format_log_entry(log))
        
        elif args.command == 'stats':
            stats = manager.get_log_statistics(hours=args.hours)
            
            if args.json:
                print(json.dumps(stats, indent=2))
            else:
                print(f"Log Statistics (last {args.hours} hours):")
                print(f"Total logs: {stats['total_logs']}")
                print("\nLevel distribution:")
                for level, count in stats['level_distribution'].items():
                    print(f"  {level}: {count}")
                print("\nTop loggers:")
                for logger, count in stats['top_loggers'].items():
                    print(f"  {logger}: {count}")
        
        elif args.command == 'cleanup':
            if args.dry_run:
                # For dry run, we'll count how many logs would be deleted
                cutoff_date = datetime.now() - timedelta(days=args.days)
                session = manager.Session()
                try:
                    count = session.query(manager.LogEntry).filter(
                        manager.LogEntry.timestamp < cutoff_date
                    ).count()
                    print(f"Would delete {count} logs older than {args.days} days")
                finally:
                    session.close()
            else:
                deleted_count = manager.cleanup_old_logs(days=args.days)
                print(f"Deleted {deleted_count} logs older than {args.days} days")
    
    finally:
        manager.close()

if __name__ == '__main__':
    main() 