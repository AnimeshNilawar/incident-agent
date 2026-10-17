from fastapi import FastAPI

app = FastAPI(
    title="Incident Agent",
    description="Automated incident investigation",
    version="0.1.0",
)

@app.get("/health/live")
def health_live():
    return {"status": "alive"}

@app.get("/")
def root():
    return{
        "service": "incident-agent",
        "status": "running",
    }