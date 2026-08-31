import uuid
import os

def create_session():
    session_id = str(uuid.uuid4())

    os.makedirs(f"uploads/{session_id}", exist_ok=True)
    os.makedirs(f"output/{session_id}", exist_ok=True)

    return session_id

