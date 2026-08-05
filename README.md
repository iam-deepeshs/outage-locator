# ⚡ Outage Locator – Smart Fault Detection & Localization System

A full-stack web application for detecting, localizing, and managing low-tension
power outages using simulated pole-level telemetry for the fictional
Karnataka State Power Distribution Board (KSPDB).

## 🚀 Features

- Real-time outage detection from telemetry
- Intelligent fault localization
- Interactive network visualization
- Incident & ticket management workflow
- Scheduled outage suppression
- Dead sensor detection
- Synthetic network simulator
- AI-powered dispatch summaries (with graceful fallback)
- REST API with Swagger documentation
- Dockerized deployment
- Live deployment on Render

---

## 🛠 Tech Stack

### Frontend
- React (Vite)
- JavaScript
- CSS

### Backend
- FastAPI
- Python
- SQLAlchemy
- Pydantic

### Database
- PostgreSQL

### Deployment
- Docker
- Docker Compose
- Render

---

## 📂 Project Structure

```
backend/
frontend/
docker-compose.yml
render.yaml
ARCHITECTURE.md
DEPLOYMENT.md
DECISIONS.md
AI-WORKFLOW.md
```

---

## ⚙️ Local Setup

```bash
git clone https://github.com/iam-deepeshs/outage-locator.git

cd outage-locator

docker compose up --build
```

Backend

```
http://localhost:8000
```

Swagger

```
http://localhost:8000/docs
```

Frontend

```
http://localhost:5173
```

---

## 🌐 Live Deployment

Frontend:
https://outage-locator-frontend-4bna.onrender.com

Backend API:
https://outage-locator-backend-3b6c.onrender.com

Swagger:
https://outage-locator-backend-3b6c.onrender.com/docs


## 📹 Demo Video

https://youtu.be/iA8N8GmuUoY

---

## 📄 Documentation

- ARCHITECTURE.md
- DEPLOYMENT.md
- DECISIONS.md
- AI-WORKFLOW.md

---

## 📌 Current Status

- ✅ Dockerized
- ✅ PostgreSQL Integrated
- ✅ Render Deployment
- ✅ REST APIs
- ✅ Interactive Dashboard
- ✅ Fault Simulator
- ✅ Ticket Lifecycle
- ✅ AI Summary Integration

---

## 👨‍💻 Author

Deepesh Srivastava

GitHub:
https://github.com/iam-deepeshs
