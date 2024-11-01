import pandas as pd
import joblib
import requests
from dateutil import parser
import json
import numpy as np

# Configuration
vaultDNS = "partnersi-prana4life-clinical.veevavault.com"
version = "v24.1"
username = "rudhrainfosolutions@partnersi-prana4life.com"
password = "Rudhrainfo#05"

# Load the trained XGBoost model
xg_reg = joblib.load('/content/xgboost_model_study.pkl')

# Function to convert date to ordinal
def date_to_ordinal(date_str):
    try:
        return pd.to_datetime(date_str).toordinal()
    except ValueError:
        return np.nan

# Construct the endpoint URL for authentication
auth_url = f"https://{vaultDNS}/api/{version}/auth"

# Prepare the payload for authentication
auth_payload = {
    'username': username,
    'password': password
}

# Function to authenticate and return session_id
def authenticate():
    auth_response = requests.post(auth_url, data=auth_payload)
    if auth_response.status_code == 200:
        print("Authentication successful!")
        return auth_response.json().get('sessionId')
    else:
        print("Authentication failed!")
        print(f"Error details: {auth_response.text}")
        return None

# Function to fetch data from the vault
def fetch_data(session_id):
    query_url = f"https://{vaultDNS}/api/{version}/query"
    query_payload = {
        'q': ("select id,name__v,study__v,study_country__vr.name__v,study__vr.name__v,"
              "study__vr.ai_predicted_end_date__c,study__vr.study_name__v,study__vr.study_start_date__c,"
              "study__vr.therapeutic_area__c,study__vr.study_planned_start_date__c,study__vr.planned_study_end_date__c,"
              "(select id,name__v,forecast__ctms,planned__ctms,actual__ctms from metrics__ctmsr) "
              "from site__v WHERE study__vr.status__v = 'active__v' ")
    }

    query_headers = {
        'Authorization': session_id,
        'Accept': 'application/json',
        'X-VaultAPI-DescribeQuery': 'true',
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    offset = 0
    limit = 1000
    all_records = []

    while True:
        query_payload['q'] += f" pagesize {limit} pageoffset {offset} "
        print("Query Payload:", query_payload)
        query_response = requests.post(query_url, headers=query_headers, data=query_payload)

        if query_response.status_code == 200:
            print("Query was successful!")
            response_data = query_response.json()
            print("Response Data:", response_data)

            if 'data' not in response_data:
                print("No data found in response")
                break

            records = response_data.get('data', [])
            all_records.extend(records)

            # Check if there are more records to fetch
            response_details = response_data.get('responseDetails')
            if response_details:
                total_size = response_details.get('total', 0)
                print("Total Size:", total_size)
                if offset + limit >= total_size:
                    break
                offset += limit
            else:
                print("Response details not found, stopping query")
                break

        else:
            print("Query failed!")
            print(f"Error details: {query_response.text}")
            break

        # Reset the query string for the next iteration
        end_position = query_payload['q'].find("from site__v") + len("from site__v")
        cleaned_query = query_payload['q'][:end_position]
        query_payload['q'] = cleaned_query

    print("Fetched Data:", len(all_records))  # Debugging step
    return all_records

# Function to process data and make predictions using XGBoost
def process_data(data):
    results = []

    for event in data:
        study_id = event.get('id')
        name = event.get('name__v')
        planned_study_start_date = event.get('study__vr.study_planned_start_date__c')
        study_start_date = event.get('study__vr.study_start_date__c')
        planned_study_end_date = event.get('study__vr.planned_study_end_date__c')
        ai_predicted_end_date = event.get('study__vr.ai_predicted_end_date__c')
        metrics__ctmsr = event.get('metrics__ctmsr', {})

        metrics_data = metrics__ctmsr.get('data', [])

        # Initialize variables
        planned__ctms = None
        actual__ctms_enrolled = None
        actual__ctms_dropout = None

        # Extract metrics data
        for metrics in metrics_data:
            metric_name = metrics.get('name__v')
            if metric_name == 'Total Enrolled':
                planned__ctms = metrics.get('planned__ctms')
            if metric_name == 'Total In Active Enrollment':
                actual__ctms_enrolled = metrics.get('actual__ctms')
            if metric_name == 'Total Withdrawn':
                actual__ctms_dropout = metrics.get('actual__ctms')

        # Ensure all required data is available
        if planned_study_start_date and study_start_date and planned_study_end_date and planned__ctms is not None and actual__ctms_enrolled is not None and actual__ctms_dropout is not None:
            # Predict using the XGBoost model
            predicted_end_date = predict_end_date(
                planned_study_end_date,
                planned_study_start_date,
                study_start_date,
                actual__ctms_enrolled,
                planned__ctms,
                actual__ctms_dropout
            )

            # Update the ai_predicted_end_date with the predicted value
            event['study__vr.ai_predicted_end_date__c'] = predicted_end_date.strftime('%Y-%m-%d')
            results.append(event)

    print("Results before updating Vault:", len(results))
    return results

# Function to predict end date using XGBoost
def predict_end_date(planned_end_date, planned_start_date, actual_start_date, enrolled_subjects, planned_subjects, drop_outs):
    # Convert input dates to ordinal
    data = {
        'planned_end_date_num': [date_to_ordinal(planned_end_date)],
        'planned_start_date_num': [date_to_ordinal(planned_start_date)],
        'Study Actual Start Date_num': [date_to_ordinal(actual_start_date)],
        'Study Enrolled Subjects': [enrolled_subjects],
        'Study Planned Subject': [planned_subjects],
        'Study Drop-Outs': [drop_outs]
    }
    input_df = pd.DataFrame(data)

    # Make prediction
    predicted_ordinal = xg_reg.predict(input_df)[0]

    # Convert predicted ordinal back to date
    predicted_date = pd.to_datetime(pd.Timestamp.fromordinal(int(predicted_ordinal)))
    return predicted_date

# Function to update records in Vault
def update_vault(session_id, updated_records):
    # Construct the endpoint URL for updating records
    update_url = f"https://{vaultDNS}/api/{version}/vobjects/study__v"

    # Prepare the headers for the update
    update_headers = {
        'Authorization': session_id,
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        "X-VaultAPI-MigrationMode": "true"
    }

    # Loop through the updated records and send them to Vault
    records_list=[]
    study_id = []
    i = 0
    for record in updated_records:
       i=i+1
       if record['study__v'] not in study_id:
          records_list.append({
                "id": record['study__v'],
                "ai_predicted_end_date__c": record['study__vr.ai_predicted_end_date__c']
                })
       study_id.append(record['study__v'])

    print("Records_list : ", records_list)
    bulk_update_json = json.dumps(records_list)
    update_response = requests.put(update_url, headers=update_headers, data=bulk_update_json)
    if update_response.status_code == 200:
        print(f"Record updated successfully!: {update_response.text}")
    else:
        print(f"Failed to update record. Status code: {update_response.status_code}, Response: {update_response.text}")

# Main flow
if __name__ == "__main__":
    # Step 1: Authenticate
    session_id = authenticate()

    if session_id:
        # Step 2: Fetch data
        data = fetch_data(session_id)

        if data:
            # Step 3: Process data and get results using XGBoost
            results = process_data(data)

            # Step 4: Update Vault with the new predictions
            update_vault(session_id, results)
