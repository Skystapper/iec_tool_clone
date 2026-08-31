import requests
import base64

url = "https://api.abby.abb.com/api/v1/developers/agent_chat/?streaming=false"
headers = {
    "X-ABBY-API-KEY":"sk-eyJ0eXAiOiAiSldUIiwgImFsZyI6ICJFUzI1NiIsICJraWQiOiAiaHR0cHM6Ly9lbGRhdGFzY2llbmNlYWJieWh1Yi52YXVsdC5henVyZS5uZXQva2V5cy9BUElLRVktU0lHTklORy1LRVktUFJPRC80MmM3Y2JhNmVkNmM0YzA0OTkwZDRkYTFlNTBmY2I2NCJ9.eyJhcHBsaWNhdGlvbl9pZCI6ICJhN2U5ZjNkNy0xZjUzLTQxNDgtYjk5OS0zNDdiM2YzZjNmNTYiLCAianRpIjogIjgxNzE4ZGU3LTY1MGUtNDQ1Ny1hYTI0LWUwMjE5YjdlM2YxNSIsICJleHAiOiAyMDc2MDU2NjY4fQ.KNYfrJ8EDxg5VBnJXUyxbTgir61ojT5bdIpqtRhKUJcZByVUX_5WVCp4wUxzxYoAxe_Zph2Dp4dYZqsSB3jPwQ",
    "aplication_id": "a7e9f3d7-1f53-4148-b999-347b3f3f6c56",
    "Content-Type": "application/json"
}

# Read file and encode as base64
with open(r"C:\Users\INANSPA\OneDrive - ABB\Internship\DB_ABBY_Agent\PythonCode\iec_tool\uploads\SR-1\CMES4_Product_Security_Context.pdf", "rb") as f:
    file_data = base64.b64encode(f.read()).decode()

data = {
    "agent_id": "692acc1f-3a74-4a1c-bc49-72f953c72d2a",
    "input":
      {
        "role": "user",
        "content": [
          {
            "type": "input_text",
            "text": "Extract and summarize the main topics from this document"
          },
          {
            "type": "input_file",
            "file":
              {
                "data": file_data,
                "mimetype": "application/pdf"
              }
          }
        ]
      }}
response = requests.post(url, headers=headers, json=data)
print(response.json())