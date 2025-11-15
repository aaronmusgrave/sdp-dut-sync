def format_ticket_for_dust(ticket, detailed_ticket=None, conversations=None):
    """Format ticket as markdown for Dust"""
    ticket_id = ticket.get('id', 'Unknown')
    subject = ticket.get('subject', 'No Subject')
    
    ticket_data = detailed_ticket if detailed_ticket else ticket
    
    status = ticket_data.get('status', {}) or {}
    status_name = status.get('name', 'Unknown')
    
    priority = ticket_data.get('priority', {}) or {}
    priority_name = priority.get('name', 'Unknown')
    
    requester = ticket_data.get('requester', {}) or {}
    requester_name = requester.get('name', 'Unknown')
    requester_email = requester.get('email_id', '')
    
    technician = ticket_data.get('technician', {}) or {}
    technician_name = technician.get('name', 'Unassigned')
    
    created_time = ticket_data.get('created_time', {}) or {}
    created_time_display = created_time.get('display_value', 'Unknown')
    
    due_by_time = ticket_data.get('due_by_time', {}) or {}
    due_by_time_display = due_by_time.get('display_value', 'Not set')
    
    category = ticket_data.get('category', {}) or {}
    category_name = category.get('name', 'Uncategorized')
    
    description = clean_html(ticket_data.get('description', 'No description'))
    
    resolution = ticket_data.get('resolution', {}) or {}
    resolution_content = clean_html(resolution.get('content', ''))
    
    attachments = ticket_data.get('attachments', []) or []
    
    text = f"""# Ticket #{ticket_id}: {subject}

## Details
- **Status**: {status_name}
- **Priority**: {priority_name}
- **Category**: {category_name}
- **Created**: {created_time_display}
- **Due By**: {due_by_time_display}

## People
- **Requester**: {requester_name} ({requester_email})
- **Assigned To**: {technician_name}

## Description
{description}
"""

    if resolution_content:
        text += f"\n## Resolution\n{resolution_content}\n"

    if attachments:
        text += "\n## Attachments\n"
        for att in attachments:
            name = att.get('name', 'Unknown')
            size_raw = att.get('size', 0)
            # Handle size as either string or int
            try:
                size_kb = float(size_raw) / 1024 if size_raw else 0
            except (ValueError, TypeError):
                size_kb = 0
            
            if size_kb > 0:
                text += f"- {name} ({size_kb:.1f} KB)\n"
            else:
                text += f"- {name}\n"

    if conversations:
        text += "\n## Comments & Updates\n\n"
        for conv in conversations:
            from_user = (conv.get('from', {}) or {}).get('name', 'Unknown')
            time_val = (conv.get('created_time', {}) or {}).get('display_value', 'Unknown')
            content = clean_html(conv.get('description', '') or conv.get('reply_content', ''))
            
            if content:
                text += f"### {from_user} - {time_val}\n{content}\n\n"

    text += f"\n---\n*Last synced: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}*\n"
    return text
