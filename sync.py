name: ServiceDesk Plus Sync

on:
  workflow_dispatch:
    inputs:
      sync_type:
        description: 'Sync type'
        required: false
        default: 'regular'
        type: choice
        options:
          - regular
          - csv_import
  schedule:
    - cron: '0 * * * *'

jobs:
  sync:
    runs-on: ubuntu-latest
    
    steps:
    - name: Checkout code
      uses: actions/checkout@v3
    
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'
    
    - name: Install dependencies
      run: |
        pip install requests pandas openpyxl
    
    - name: Run sync
      env:
        SDP_DOMAIN: ${{ secrets.SDP_DOMAIN }}
        SDP_CLIENT_ID: ${{ secrets.SDP_CLIENT_ID }}
        SDP_CLIENT_SECRET: ${{ secrets.SDP_CLIENT_SECRET }}
        SDP_REFRESH_TOKEN: ${{ secrets.SDP_REFRESH_TOKEN }}
        DUST_API_KEY: ${{ secrets.DUST_API_KEY }}
        DUST_WORKSPACE_ID: ${{ secrets.DUST_WORKSPACE_ID }}
        DUST_FOLDER_NAME: ${{ secrets.DUST_FOLDER_NAME }}
      run: |
        if [ "${{ github.event.inputs.sync_type }}" = "csv_import" ]; then
          echo "Running CSV import..."
          python import_from_csv.py
        else
          echo "Running regular hourly sync..."
          python sync.py
        fi
