"""起動スクリプト:  python run.py"""
import logging

import uvicorn

from app.config import settings
from app.main import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

if __name__ == "__main__":
    uvicorn.run(create_app(), host=settings.host, port=settings.port, log_level="warning")
