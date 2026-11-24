"""Authenticated human workspace creation, with optional chat selection."""
import hal_permissions

def create(connection,data,studio,add):
    permissions=hal_permissions.normalize(data.get('permissions'),hal_permissions.DEFAULT)
    source=data.get('source')
    if source=='new' and 'auto_commit' not in (data.get('permissions') or {}):permissions['auto_commit']=True
    if source not in ('new','existing'):raise ValueError('Choose a new or existing Gitea project')
    chat=data.get('id','')
    if chat:
        connection.execute('BEGIN IMMEDIATE')
        if not connection.execute('SELECT 1 FROM chats WHERE id=?',(chat,)).fetchone():raise ValueError('Conversation not found')
        if connection.execute("SELECT 1 FROM turns WHERE chat=? AND state IN ('queued','running','stopping')",(chat,)).fetchone():
            raise ValueError('Stop or finish the active reply before changing its workspace')
    if source=='new':
        hal_permissions.require('create_project',permissions)
        workspace=studio('create_project',{'name':data.get('project_name',''),'branch':data.get('branch','')},human=True,permissions=permissions)
    else:
        hal_permissions.require('checkout_repository',permissions)
        workspace=studio('checkout_repository',{'repository':data.get('repository',''),'base_branch':data.get('base_branch',''),'branch':data.get('branch','')},human=True,permissions=permissions)
    if chat:
        connection.execute('UPDATE chats SET workspace=? WHERE id=?',(workspace['id'],chat))
        hal_permissions.save(connection,chat,permissions)
        add(connection,chat,'workspace','Selected '+workspace['repository']+' · '+workspace['branch'])
    return workspace
