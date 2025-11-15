- name: Run CSV import (manual only)
  if: github.event.inputs.sync_type == 'csv_import'
  env:
    SDP_DOMAIN: ${{ secrets.SDP_DOMAIN }}
    SDP_CLIENT_ID: ${{ secrets.SDP_CLIENT_ID }}
    SDP_CLIENT_SECRET: ${{ secrets.SDP_CLIENT_SECRET }}
    SDP_REFRESH_TOKEN: ${{ secrets.SDP_REFRESH_TOKEN }}
    DUST_API_KEY: ${{ secrets.DUST_API_KEY }}
    DUST_WORKSPACE_ID: ${{ secrets.DUST_WORKSPACE_ID }}
    DUST_FOLDER_NAME: ${{ secrets.DUST_FOLDER_NAME }}
  run: python import_from_csv.py
