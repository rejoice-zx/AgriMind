"""
JSON to SQLite Migration Script

Migrates data from JSON files to SQLite database:
- users.json -> users table
- knowledge_base.json -> knowledge_items table
- products.json -> products table
- conversations/*.json -> conversations and messages tables
- settings.json -> system_settings table

Requirements: 14.3
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.database import AsyncSessionLocal, init_db
from app.models.user import User
from app.models.conversation import Conversation, Message
from app.models.knowledge import KnowledgeItem
from app.models.product import Product
from app.models.settings import SystemSettings


class MigrationResult:
    """Result of a migration operation"""
    def __init__(self):
        self.success = True
        self.migrated_count = 0
        self.skipped_count = 0
        self.error_count = 0
        self.errors: List[str] = []
    
    def add_error(self, error: str):
        self.errors.append(error)
        self.error_count += 1
        self.success = False
    
    def __repr__(self):
        return (f"MigrationResult(success={self.success}, "
                f"migrated={self.migrated_count}, "
                f"skipped={self.skipped_count}, "
                f"errors={self.error_count})")


class JSONToSQLiteMigrator:
    """Migrates JSON data files to SQLite database"""
    
    def __init__(self, data_dir: str = "./data"):
        self.data_dir = Path(data_dir)
        self.results: Dict[str, MigrationResult] = {}
    
    def _parse_datetime(self, dt_str: Optional[str]) -> Optional[datetime]:
        """Parse datetime string to datetime object"""
        if not dt_str:
            return None
        try:
            # Try common formats
            for fmt in [
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d",
            ]:
                try:
                    return datetime.strptime(dt_str, fmt)
                except ValueError:
                    continue
            return None
        except Exception:
            return None
    
    async def migrate_users(self, session: AsyncSession) -> MigrationResult:
        """Migrate users.json to users table"""
        result = MigrationResult()
        users_file = self.data_dir / "users.json"
        
        if not users_file.exists():
            result.add_error(f"Users file not found: {users_file}")
            return result
        
        try:
            with open(users_file, "r", encoding="utf-8") as f:
                users_data = json.load(f)
            
            for username, user_info in users_data.items():
                try:
                    # Check if user already exists
                    existing = await session.execute(
                        select(User).where(User.username == username)
                    )
                    if existing.scalar_one_or_none():
                        result.skipped_count += 1
                        continue
                    
                    # Create password hash from old format
                    # Old format stores algo, salt, iterations, hash
                    password_data = user_info.get("password", {})
                    if isinstance(password_data, dict):
                        # Store the old hash format as-is for now
                        # In production, you'd want to re-hash with bcrypt
                        password_hash = json.dumps(password_data)
                    else:
                        password_hash = str(password_data)
                    
                    user = User(
                        username=username,
                        password_hash=password_hash,
                        role=user_info.get("role", "cs"),
                        display_name=user_info.get("name", username),
                        email=user_info.get("email"),
                        is_active=user_info.get("is_active", True),
                        created_at=self._parse_datetime(user_info.get("created_at")),
                    )
                    session.add(user)
                    result.migrated_count += 1
                except Exception as e:
                    result.add_error(f"Error migrating user {username}: {str(e)}")
            
            await session.commit()
        except Exception as e:
            result.add_error(f"Error reading users file: {str(e)}")
            await session.rollback()
        
        return result

    async def migrate_knowledge(self, session: AsyncSession) -> MigrationResult:
        """Migrate knowledge_base.json to knowledge_items table"""
        result = MigrationResult()
        knowledge_file = self.data_dir / "knowledge_base.json"
        
        if not knowledge_file.exists():
            result.add_error(f"Knowledge file not found: {knowledge_file}")
            return result
        
        try:
            with open(knowledge_file, "r", encoding="utf-8") as f:
                knowledge_data = json.load(f)
            
            items = knowledge_data.get("items", [])
            
            for item in items:
                try:
                    item_id = item.get("id")
                    if not item_id:
                        result.add_error("Knowledge item missing ID")
                        continue
                    
                    # Check if item already exists
                    existing = await session.execute(
                        select(KnowledgeItem).where(KnowledgeItem.id == item_id)
                    )
                    if existing.scalar_one_or_none():
                        result.skipped_count += 1
                        continue
                    
                    knowledge_item = KnowledgeItem(
                        id=item_id,
                        question=item.get("question", ""),
                        answer=item.get("answer", ""),
                        keywords=item.get("keywords", []),
                        category=item.get("category", "通用"),
                        score=item.get("score", 1.0),
                    )
                    session.add(knowledge_item)
                    result.migrated_count += 1
                except Exception as e:
                    result.add_error(f"Error migrating knowledge item {item.get('id')}: {str(e)}")
            
            await session.commit()
        except Exception as e:
            result.add_error(f"Error reading knowledge file: {str(e)}")
            await session.rollback()
        
        return result
    
    async def migrate_products(self, session: AsyncSession) -> MigrationResult:
        """Migrate products.json to products table"""
        result = MigrationResult()
        products_file = self.data_dir / "products.json"
        
        if not products_file.exists():
            result.add_error(f"Products file not found: {products_file}")
            return result
        
        try:
            with open(products_file, "r", encoding="utf-8") as f:
                products_data = json.load(f)
            
            items = products_data.get("products", [])
            
            for item in items:
                try:
                    item_id = item.get("id")
                    if not item_id:
                        result.add_error("Product missing ID")
                        continue
                    
                    # Check if product already exists
                    existing = await session.execute(
                        select(Product).where(Product.id == item_id)
                    )
                    if existing.scalar_one_or_none():
                        result.skipped_count += 1
                        continue
                    
                    product = Product(
                        id=item_id,
                        name=item.get("name", ""),
                        price=item.get("price", 0.0),
                        category=item.get("category", ""),
                        description=item.get("description", ""),
                        specifications=item.get("specifications", {}),
                        stock=item.get("stock", 0),
                        keywords=item.get("keywords", []),
                    )
                    session.add(product)
                    result.migrated_count += 1
                except Exception as e:
                    result.add_error(f"Error migrating product {item.get('id')}: {str(e)}")
            
            await session.commit()
        except Exception as e:
            result.add_error(f"Error reading products file: {str(e)}")
            await session.rollback()
        
        return result

    async def migrate_conversations(self, session: AsyncSession) -> MigrationResult:
        """Migrate conversations/*.json to conversations and messages tables"""
        result = MigrationResult()
        conversations_dir = self.data_dir / "conversations"
        
        if not conversations_dir.exists():
            result.add_error(f"Conversations directory not found: {conversations_dir}")
            return result
        
        try:
            conversation_files = list(conversations_dir.glob("*.json"))
            
            for conv_file in conversation_files:
                try:
                    with open(conv_file, "r", encoding="utf-8") as f:
                        conv_data = json.load(f)
                    
                    conv_id = conv_data.get("id")
                    if not conv_id:
                        result.add_error(f"Conversation missing ID in {conv_file}")
                        continue
                    
                    # Check if conversation already exists
                    existing = await session.execute(
                        select(Conversation).where(Conversation.id == conv_id)
                    )
                    if existing.scalar_one_or_none():
                        result.skipped_count += 1
                        continue
                    
                    # Create conversation
                    conversation = Conversation(
                        id=conv_id,
                        title=conv_data.get("title", ""),
                        status=conv_data.get("status", "normal"),
                        human_agent_id=conv_data.get("human_agent_id"),
                        created_at=self._parse_datetime(conv_data.get("created_at")),
                        updated_at=self._parse_datetime(conv_data.get("updated_at")),
                    )
                    session.add(conversation)
                    
                    # Create messages
                    messages = conv_data.get("messages", [])
                    for msg_data in messages:
                        message = Message(
                            conversation_id=conv_id,
                            role=msg_data.get("role", "user"),
                            content=msg_data.get("content", ""),
                            confidence=msg_data.get("confidence"),
                            rag_trace=msg_data.get("rag_trace"),
                            timestamp=self._parse_datetime(msg_data.get("timestamp")),
                        )
                        session.add(message)
                    
                    result.migrated_count += 1
                except Exception as e:
                    result.add_error(f"Error migrating conversation {conv_file}: {str(e)}")
            
            await session.commit()
        except Exception as e:
            result.add_error(f"Error reading conversations: {str(e)}")
            await session.rollback()
        
        return result
    
    async def migrate_settings(self, session: AsyncSession) -> MigrationResult:
        """Migrate settings.json to system_settings table"""
        result = MigrationResult()
        settings_file = self.data_dir / "settings.json"
        
        if not settings_file.exists():
            result.add_error(f"Settings file not found: {settings_file}")
            return result
        
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                settings_data = json.load(f)
            
            for key, value in settings_data.items():
                try:
                    # Check if setting already exists
                    existing = await session.execute(
                        select(SystemSettings).where(SystemSettings.key == key)
                    )
                    if existing.scalar_one_or_none():
                        result.skipped_count += 1
                        continue
                    
                    # Convert value to string for storage
                    if isinstance(value, (dict, list)):
                        value_str = json.dumps(value)
                    else:
                        value_str = str(value)
                    
                    setting = SystemSettings(
                        key=key,
                        value=value_str,
                    )
                    session.add(setting)
                    result.migrated_count += 1
                except Exception as e:
                    result.add_error(f"Error migrating setting {key}: {str(e)}")
            
            await session.commit()
        except Exception as e:
            result.add_error(f"Error reading settings file: {str(e)}")
            await session.rollback()
        
        return result

    async def run_all_migrations(self, clear_existing: bool = False) -> Dict[str, MigrationResult]:
        """Run all migrations"""
        if clear_existing:
            raise ValueError("已停用清空现有数据迁移；请使用独立目标数据库")
        # Initialize database
        await init_db()
        
        async with AsyncSessionLocal() as session:
            # Run migrations
            self.results["users"] = await self.migrate_users(session)
            self.results["knowledge"] = await self.migrate_knowledge(session)
            self.results["products"] = await self.migrate_products(session)
            self.results["conversations"] = await self.migrate_conversations(session)
            self.results["settings"] = await self.migrate_settings(session)
        
        return self.results
    
    def print_results(self):
        """Print migration results"""
        print("\n" + "=" * 60)
        print("Migration Results")
        print("=" * 60)
        
        total_migrated = 0
        total_skipped = 0
        total_errors = 0
        all_success = True
        
        for name, result in self.results.items():
            status = "✓" if result.success else "✗"
            print(f"\n{status} {name.upper()}")
            print(f"  Migrated: {result.migrated_count}")
            print(f"  Skipped:  {result.skipped_count}")
            print(f"  Errors:   {result.error_count}")
            
            if result.errors:
                for error in result.errors[:5]:  # Show first 5 errors
                    print(f"    - {error}")
                if len(result.errors) > 5:
                    print(f"    ... and {len(result.errors) - 5} more errors")
            
            total_migrated += result.migrated_count
            total_skipped += result.skipped_count
            total_errors += result.error_count
            if not result.success:
                all_success = False
        
        print("\n" + "-" * 60)
        print(f"TOTAL: Migrated={total_migrated}, Skipped={total_skipped}, Errors={total_errors}")
        print(f"STATUS: {'SUCCESS' if all_success else 'COMPLETED WITH ERRORS'}")
        print("=" * 60 + "\n")
        
        return all_success


async def main():
    """Main entry point for migration script"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Migrate JSON data to SQLite database")
    parser.add_argument(
        "--data-dir",
        default="./data",
        help="Path to data directory containing JSON files"
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear existing data before migration"
    )
    
    args = parser.parse_args()
    
    print(f"Starting migration from {args.data_dir}")
    if args.clear:
        print("WARNING: Existing data will be cleared!")
    
    migrator = JSONToSQLiteMigrator(data_dir=args.data_dir)
    await migrator.run_all_migrations(clear_existing=args.clear)
    success = migrator.print_results()
    
    return 0 if success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
