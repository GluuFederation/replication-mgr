searchlist = {
'total_connections':('cn=Total,cn=Connections,cn=Monitor','monitorCounter', '#'),
'bytes_sent': ('cn=Bytes,cn=Statistics,cn=Monitor','monitorCounter','Bytes'),
'completed_operations': ('cn=Operations,cn=Monitor','monitorOpCompleted', '#'),
'initiated_operations': ('cn=Operations,cn=Monitor','monitorOpInitiated', '#'),
'referrals_sent': ('cn=Referrals,cn=Statistics,cn=Monitor','monitorCounter', '#'),
'entries_sent': ('cn=Entries,cn=Statistics,cn=Monitor','monitorCounter', '#'),
'bind_operations': ('cn=Bind,cn=Operations,cn=Monitor','monitorOpCompleted', '#'),
'unbind_operations': ('cn=Unbind,cn=Operations,cn=Monitor','monitorOpCompleted', '#'),
'add_operations': ('cn=Add,cn=Operations,cn=Monitor','monitorOpInitiated', '#'),
'delete_operations':  ('cn=Delete,cn=Operations,cn=Monitor','monitorOpCompleted', '#'),
'modify_operations': ('cn=Modify,cn=Operations,cn=Monitor','monitorOpCompleted', '#'),
'compare_operations': ('cn=Compare,cn=Operations,cn=Monitor','monitorOpCompleted', '#'),
'search_operations': ('cn=Search,cn=Operations,cn=Monitor','monitorOpCompleted', '#'),
'write_waiters': ('cn=Write,cn=Waiters,cn=Monitor','monitorCounter', '#'),
'read_waiters': ('cn=Read,cn=Waiters,cn=Monitor','monitorCounter', '#'),
}