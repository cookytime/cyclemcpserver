import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # base44 API settings
    BASE44_API_KEY = os.getenv('BASE44_API_KEY')
    BASE44_API_URL = os.getenv('BASE44_API_URL', 'https://app.base44.com/api')
    BASE44_APP_ID = os.getenv('BASE44_APP_ID')

    # PostgreSQL settings
    DB_HOST = os.getenv('DB_HOST', 'localhost')
    DB_PORT = os.getenv('DB_PORT', '5432')
    DB_NAME = os.getenv('DB_NAME', 'choreography')
    DB_USER = os.getenv('DB_USER')
    DB_PASSWORD = os.getenv('DB_PASSWORD')

    @classmethod
    def get_db_connection_string(cls):
        return f"host={cls.DB_HOST} port={cls.DB_PORT} dbname={cls.DB_NAME} user={cls.DB_USER} password={cls.DB_PASSWORD}"

    @classmethod
    def validate(cls):
        """Validate that all required configuration is present"""
        required = [
            ('BASE44_API_KEY', cls.BASE44_API_KEY),
            ('BASE44_APP_ID', cls.BASE44_APP_ID),
            ('DB_USER', cls.DB_USER),
            ('DB_PASSWORD', cls.DB_PASSWORD),
        ]

        missing = [name for name, value in required if not value]

        if missing:
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")

        return True
