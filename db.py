from pymongo import MongoClient

from config import settings


_client = MongoClient(settings.MONGODB_URI)
_db = _client[settings.MONGODB_DB_NAME]

# Collections
jobs_collection = _db[settings.MONGODB_JOBS_COLLECTION]
users_collection = _db["users"]
access_requests_collection = _db["access_requests"]

