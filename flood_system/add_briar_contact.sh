#!/bin/bash
# add_briar_contact.sh

if [ -z "$1" ] || [ -z "$2" ]; then
    echo "Usage: ./add_briar_contact.sh <ALIAS> <BRIAR_LINK>"
    exit 1
fi

ALIAS=$1
LINK=$2
TOKEN="awKMAuJBv+DVPg8OHaJNOWDZLQzdkLCxWUwH9cru7b4="
API="http://127.0.0.1:7000/v1"

echo "Adding contact '$ALIAS'..."
curl -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d "{\"link\": \"$LINK\", \"alias\": \"$ALIAS\"}" \
     $API/contacts/add/pending

echo -e "\nContact request sent!"
