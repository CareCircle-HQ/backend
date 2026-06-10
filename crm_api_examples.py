import asyncio
import os

from dotenv import load_dotenv
from highlevel import HighLevel

load_dotenv()

CLIENT_ID = os.getenv("GHL_CLIENT_ID")
CLIENT_SECRET = os.getenv("GHL_CLIENT_SECRET")
LOCATION_ID = os.getenv("GHL_LOCATION_ID")
PRIVATE_TOKEN = os.getenv("GHL_PRIVATE_TOKEN")


import requests


## Get Contact
url = "https://services.leadconnectorhq.com/contacts/:contactId"

payload = {}
headers = {
  'Accept': 'application/json',
  'Authorization': 'Bearer <TOKEN>'
}

response = requests.request("GET", url, headers=headers, data=payload)

print(response.text)

## Create a new contact

payload = json.dumps({
  "firstName": "Rosan",
  "lastName": "Deo",
  "name": "Rosan Deo",
  "email": "rosan@deos.com",
  "locationId": "ve9EPM428h8vShlRW1KT",
  "gender": "male",
  "phone": "+1 888-888-8888",
  "address1": "3535 1st St N",
  "city": "Dolomite",
  "state": "AL",
  "postalCode": "35061",
  "website": "https://www.tesla.com",
  "timezone": "America/Chihuahua",
  "dnd": True,
  "dndSettings": {
    "Call": {
      "status": "active",
      "message": "Do not call"
    },
    "Email": {
      "status": "inactive"
    }
  },
  "inboundDndSettings": {
    "all": {
      "status": "active",
      "message": "Do not contact me"
    }
  },
  "tags": [
    "nisi sint commodo amet",
    "consequat"
  ],
  "customFields": [
    {
      "id": "6dvNaf7VhkQ9snc5vnjJ",
      "key": "my_custom_field",
      "fieldValue": "My Text"
    }
  ],
  "source": "public api",
  "dateOfBirth": "1990-09-25",
  "country": "US",
  "companyName": "DGS VolMAX",
  "assignedTo": "y0BeYjuRIlDwsDcOHOJo"
})
headers = {
  'Content-Type': 'application/json',
  'Accept': 'application/json',
  'Authorization': 'Bearer <TOKEN>'
}

response = requests.request("POST", url, headers=headers, data=payload)


## Update a contact using contactId
payload = json.dumps({
  "firstName": "rosan",
  "lastName": "Deo",
  "name": "rosan Deo",
  "email": "rosan@deos.com",
  "phone": "+1 888-888-8888",
  "address1": "3535 1st St N",
  "city": "Dolomite",
  "state": "AL",
  "postalCode": "35061",
  "website": "https://www.tesla.com",
  "timezone": "America/Chihuahua",
  "dnd": True,
  "dndSettings": {
    "Call": {
      "status": "active",
      "message": "Do not call"
    },
    "Email": {
      "status": "inactive"
    }
  },
  "inboundDndSettings": {
    "all": {
      "status": "active",
      "message": "Do not contact me"
    }
  },
  "tags": [
    "nisi sint commodo amet",
    "consequat"
  ],
  "customFields": [
    {
      "id": "6dvNaf7VhkQ9snc5vnjJ",
      "key": "my_custom_field",
      "fieldValue": "My Text"
    }
  ],
  "source": "public api",
  "dateOfBirth": "1990-09-25",
  "country": "US",
  "assignedTo": "y0BeYjuRIlDwsDcOHOJo"
})
headers = {
  'Content-Type': 'application/json',
  'Accept': 'application/json',
  'Authorization': 'Bearer <TOKEN>'
}

response = requests.request("PUT", url, headers=headers, data=payload)

p