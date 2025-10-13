from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from util.class_object import singleton

from api.routes import routers as v1_routers
from core.config import configs
from core.container import Container


@singleton
class AppCreator:
    def __init__(self):
        # set app default
        self.app = FastAPI(
            title=configs.PROJECT_NAME,
            openapi_url=f"{configs.API}/openapi.json",
            version="0.0.1",
        )

        # set db and container
        self.container = Container()
        self.db = self.container.db()
        # create tables at startup (simple bootstrap; switch to Alembic for prod)
        self.db.create_database()

        # set cors
        if configs.BACKEND_CORS_ORIGINS:
            self.app.add_middleware(
                CORSMiddleware,
                allow_origins=[str(origin) for origin in configs.BACKEND_CORS_ORIGINS],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )


        # set routes
        @self.app.get("/")
        def root():
            return "service is working"

        self.app.include_router(v1_routers, prefix="")


app_creator = AppCreator()
app = app_creator.app
db = app_creator.db
container = app_creator.container

def main():
    import uvicorn
    import asyncio
    import sys
    
    # Fix for Windows asyncio issues
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    """Entry point để chạy uvicorn server."""
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
        loop="asyncio"
    )


if __name__ == "__main__":
    main()