# -*- coding: utf-8 -*-

import os
import re
import time

from flask import current_app as app

from clustermgr.models import Server, AppConfiguration
from clustermgr.extensions import wlogger, db, celery
from clustermgr.core.remote import RemoteClient
from clustermgr.core.ldap_functions import LdapOLC
from clustermgr.core.utils import get_setup_properties
from clustermgr.config import Config
import uuid


def run_command(tid, c, command, container=None, no_error='error'):
    """Shorthand for RemoteClient.run(). This function automatically logs
    the commands output at appropriate levels to the WebLogger to be shared
    in the web frontend.

    Args:
        tid (string): task id of the task to store the log
        c (:object:`clustermgr.core.remote.RemoteClient`): client to be used
            for the SSH communication
        command (string): the command to be run on the remote server
        container (string, optional): location where the Gluu Server container
            is installed. For standalone LDAP servers this is not necessary.

    Returns:
        the output of the command or the err thrown by the command as a string
    """
    if container == '/':
        container = None
    if container:
        command = 'chroot {0} /bin/bash -c "{1}"'.format(container,
                                                         command)

    wlogger.log(tid, command, "debug")
    cin, cout, cerr = c.run(command)
    output = ''
    if cout:
        wlogger.log(tid, cout, "debug")
        output += "\n" + cout
    if cerr:
        # For some reason slaptest decides to send success message as err, so
        if 'config file testing succeeded' in cerr:
            wlogger.log(tid, cerr, "success")
        else:
            wlogger.log(tid, cerr, no_error)
        output += "\n" + cerr

    return output


def upload_file(tid, c, local, remote):
    """Shorthand for RemoteClient.upload(). This function automatically handles
    the logging of events to the WebLogger

    Args:
        tid (string): id of the task running the command
        c (:object:`clustermgr.core.remote.RemoteClient`): client to be used
            for the SSH communication
        local (string): local location of the file to upload
        remote (string): location of the file in remote server
    """
    out = c.upload(local, remote)
    wlogger.log(tid, out, 'error' if 'Error' in out else 'success')


def download_file(tid, c, remote, local):
    """Shorthand for RemoteClient.download(). This function automatically
     handles the logging of events to the WebLogger

    Args:
        tid (string): id of the task running the command
        c (:object:`clustermgr.core.remote.RemoteClient`): client to be used
            for the SSH communication
        remote (string): location of the file in remote server
        local (string): local location of the file to upload
    """
    out = c.download(remote, local)
    wlogger.log(tid, out, 'error' if 'Error' in out else 'success')


def modifyOxLdapProperties(server, c, tid, pDict, chroot):
    """Modifes /etc/gluu/conf/ox-ldap.properties file for gluu server to look
    all ldap server.

    Args:
        c (:object: `clustermgr.core.remote.RemoteClient`): client to be used
            for the SSH communication
        tid (string): id of the task running the command
        pDict (dictionary): keys are hostname and values are comma delimated
            providers
        chroot (string): root of container
    """

    # get ox-ldap.properties file from server
    remote_file = os.path.join(chroot, 'etc/gluu/conf/ox-ldap.properties')
    ox_ldap = c.get_file(remote_file)

    temp = None

    # iterate ox-ldap.properties file and modify "servers" entry
    if ox_ldap[0]:
        fc = ''
        for l in ox_ldap[1]:
            if l.startswith('servers:'):
                l = 'servers: {0}\n'.format( pDict[server.hostname] )
            fc += l

        r = c.put_file(remote_file,fc)

        if r[0]:
            wlogger.log(tid,
                'ox-ldap.properties file on {0} modified to include '
                'all providers'.format(server.hostname),
                'success')
        else:
            temp = r[1]
    else:
        temp = ox_ldap[1]

    if temp:
        wlogger.log(tid,
                'ox-ldap.properties file on {0} was not modified to '
                'include all providers: {1}'.format(server.hostname, temp),
                'warning')




@celery.task(bind=True)
def setup_filesystem_replication(self):
    """Deploys File System replicaton
    """

    print "Setting up File System Replication started"

    tid = self.request.id

    servers = Server.query.all()
    app_config = AppConfiguration.query.first()

    chroot = '/opt/gluu-server-' + app_config.gluu_version

    for server in servers:

        print "Satrting csync2 installation on", server.hostname

        wlogger.log(tid,
                "Installing csync2 for filesystem replication on {}".format(
                            server.hostname),
                'head')

        c = RemoteClient(server.hostname, ip=server.ip)
        c.startup()


        run_cmd = "{}"
        cmd_chroot = chroot

        if server.os == 'CentOS 7' or server.os == 'RHEL 7':
            cmd_chroot = None
            run_cmd = ("ssh -o IdentityFile=/etc/gluu/keys/gluu-console -o "
                "Port=60022 -o LogLevel=QUIET -o StrictHostKeyChecking=no "
                "-o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=yes "
                "root@localhost '{}'")

        if 'Ubuntu' in server.os:
            cmd = 'localedef -i en_US -f UTF-8 en_US.UTF-8'
            run_command(tid, c, cmd, chroot)

            cmd = 'locale-gen en_US.UTF-8'
            run_command(tid, c, cmd, chroot)

            install_command = 'DEBIAN_FRONTEND=noninteractive apt-get'

            cmd = '{} update'.format(install_command)
            run_command(tid, c, cmd, chroot)

            cmd = '{} install -y apt-utils'.format(install_command)
            run_command(tid, c, cmd, chroot, no_error=None)


            cmd = '{} install -y csync2'.format(install_command)
            run_command(tid, c, cmd, chroot)


            cmd = 'apt-get install -y csync2'
            run_command(tid, c, cmd, chroot)

        elif 'CentOS' in server.os:


            cmd = run_cmd.format('yum install -y epel-release')
            run_command(tid, c, cmd, cmd_chroot, no_error=None)

            cmd = run_cmd.format('yum repolist')
            run_command(tid, c, cmd, cmd_chroot, no_error=None)

            if server.os == 'CentOS 7':
                csync_rpm = 'https://github.com/mbaser/gluu/raw/master/csync2-2.0-3.gluu.centos7.x86_64.rpm'
            if server.os == 'CentOS 6':
                csync_rpm = 'https://github.com/mbaser/gluu/raw/master/csync2-2.0-3.gluu.centos6.x86_64.rpm'

            cmd = run_cmd.format('yum install -y ' + csync_rpm)
            run_command(tid, c, cmd, cmd_chroot, no_error=None)

            cmd = run_cmd.format('service xinetd stop')
            run_command(tid, c, cmd, cmd_chroot, no_error=None)

        if server.os == 'CentOS 6':
            cmd = run_cmd.format('yum install -y crontabs')
            run_command(tid, c, cmd, cmd_chroot, no_error=None)

        cmd = run_cmd.format('rm -f /var/lib/csync2/*.db3')
        run_command(tid, c, cmd, cmd_chroot, no_error=None)

        cmd = run_cmd.format('rm -f /etc/csync2*')
        run_command(tid, c, cmd, cmd_chroot, no_error=None)


        if server.primary_server:

            key_command= [
                'csync2 -k /etc/csync2.key',
                'openssl genrsa -out /etc/csync2_ssl_key.pem 1024',
                'openssl req -batch -new -key /etc/csync2_ssl_key.pem -out '
                '/etc/csync2_ssl_cert.csr',
                'openssl x509 -req -days 3600 -in /etc/csync2_ssl_cert.csr '
                '-signkey /etc/csync2_ssl_key.pem -out /etc/csync2_ssl_cert.pem',
                ]

            for cmdi in key_command:
                cmd = run_cmd.format(cmdi)
                wlogger.log(tid, cmd, 'debug')
                run_command(tid, c, cmd, cmd_chroot, no_error=None)


            replication_user_file = os.path.join(Config.DATA_DIR,
                                    'fs_replication_paths.txt')

            sync_directories = []

            for l in open(replication_user_file).readlines():
                sync_directories.append(l.strip())


            exclude_files = [
                '/etc/gluu/conf/ox-ldap.properties',
                '/etc/gluu/conf/oxTrustLogRotationConfiguration.xml',
                '/etc/gluu/conf/openldap/salt',

                ]



            csync2_config = ['group gluucluster','{']

            all_servers = Server.query.all()

            for srv in all_servers:
                csync2_config.append('  host {};'.format(srv.hostname))

            csync2_config.append('')
            csync2_config.append('  key /etc/csync2.key;')
            csync2_config.append('')

            for d in sync_directories:
                csync2_config.append('  include {};'.format(d))

            csync2_config.append('')

            csync2_config.append('  exclude *~ .*;')

            csync2_config.append('')




            for f in exclude_files:
                csync2_config.append('  exclude {};'.format(f))


            csync2_config.append('\n'
                  '  action\n'
                  '  {\n'
                  '    logfile "/var/log/csync2_action.log";\n'
                  '    do-local;\n'
                  '  }\n'
                  )

            csync2_config.append('\n'
                  '  action\n'
                  '  {\n'
                  '    pattern /opt/gluu/jetty/identity/conf/shibboleth3/idp/*;\n'
                  '    exec "/sbin/service idp restart";\n'
                  '    exec "/sbin/service identity restart";\n'
                  '    logfile "/var/log/csync2_action.log";\n'
                  '    do-local;\n'
                  '  }\n')



            csync2_config.append('\n'
                  '  action\n'
                  '  {\n'
                  '    pattern /opt/symas/etc/openldap/schema/*;\n'
                  '    exec "/sbin/service solserver restart";\n'
                  '    logfile "/var/log/csync2_action.log";\n'
                  '    do-local;\n'
                  '  }\n')

            csync2_config.append('  backup-directory /var/backups/csync2;')
            csync2_config.append('  backup-generations 3;')



            csync2_config.append('\n  auto younger;\n')

            csync2_config.append('}')


            csync2_config = '\n'.join(csync2_config)
            remote_file = os.path.join(chroot, 'etc', 'csync2.cfg')

            wlogger.log(tid, "Uploading csync2.cfg", 'debug')

            c.put_file(remote_file,  csync2_config)


        else:
            wlogger.log(tid, "Downloading csync2.cfg, csync2.key, "
                        "csync2_ssl_cert.csr, csync2_ssl_cert.pem, and"
                        "csync2_ssl_key.pem from primary server and uploading",
                        'debug')

            down_list = ['csync2.cfg', 'csync2.key', 'csync2_ssl_cert.csr',
                    'csync2_ssl_cert.pem', 'csync2_ssl_key.pem']

            primary_server = Server.query.filter_by(primary_server=True).first()
            pc = RemoteClient(primary_server.hostname, ip=primary_server.ip)
            pc.startup()
            for f in down_list:
                remote = os.path.join(chroot, 'etc', f)
                local = os.path.join('/tmp',f)
                pc.download(remote, local)
                c.upload(local, remote)

            pc.close()

        csync2_path = '/usr/sbin/csync2'


        if 'Ubuntu' in server.os:

            fc = []
            inet_conf_file = os.path.join(chroot, 'etc','inetd.conf')
            r,f=c.get_file(inet_conf_file)
            for l in f:
                if l.startswith('csync2'):
                    l = 'csync2\tstream\ttcp\tnowait\troot\t/usr/sbin/csync2\tcsync2 -i -N {}\n'.format(server.hostname)
                fc.append(l)
            fc=''.join(fc)
            c.put_file(inet_conf_file, fc)


            cmd = '/etc/init.d/openbsd-inetd restart'
            run_command(tid, c, cmd, cmd_chroot, no_error=None)

        elif 'CentOS' in server.os:
            inetd_conf = (
                '# default: off\n'
                '# description: csync2\n'
                'service csync2\n'
                '{\n'
                'flags           = REUSE\n'
                'socket_type     = stream\n'
                'wait            = no\n'
                'user            = root\n'
                'group           = root\n'
                'server          = /usr/sbin/csync2\n'
                'server_args     = -i -N %(HOSTNAME)s\n'
                'port            = 30865\n'
                'type            = UNLISTED\n'
                '#log_on_failure += USERID\n'
                'disable         = no\n'
                '# only_from     = 192.168.199.3 192.168.199.4\n'
                '}\n')

            inet_conf_file = os.path.join(chroot, 'etc', 'xinetd.d', 'csync2')
            inetd_conf = inetd_conf % ({'HOSTNAME':server.hostname})
            c.put_file(inet_conf_file, inetd_conf)


        #cmd = '{} -xv -N {}'.format(csync2_path, server.hostname)
        #run_command(tid, c, cmd, chroot, no_error=None)



        #run time sync in every minute
        cron_file = os.path.join(chroot, 'etc', 'cron.d', 'csync2')
        c.put_file(cron_file,
            '* * * * *    root    {} -N {} -xv 2>/var/log/csync2.log\n'.format(
            csync2_path, server.hostname))

        wlogger.log(tid, 'Crontab entry was created to sync files in every minute',
                         'debug')

        if ('CentOS' in server.os) or ('RHEL' in server.os):
            cmd = 'service crond reload'
            cmd = run_cmd.format('service xinetd start')
            run_command(tid, c, cmd, cmd_chroot, no_error=None)
            cmd = run_cmd.format('service crond restart')
            run_command(tid, c, cmd, cmd_chroot, no_error='debug')

        else:
            cmd = run_cmd.format('service cron reload')
            run_command(tid, c, cmd, cmd_chroot, no_error='debug')
            cmd = run_cmd.format('service openbsd-inetd restart')
            run_command(tid, c, cmd, cmd_chroot, no_error='debug')

        c.close()

    return True

@celery.task(bind=True)
def setup_ldap_replication(self, server_id):
    """Deploys ldap replicaton

    Args:
        server_id (integer): id of server to be deployed replication
    """

    print "Setting up LDAP Replication started"

    tid = self.request.id
    app_config = AppConfiguration.query.first()

    servers_to_deploy = []

    if not server_id == 'all':
        server = Server.query.get(server_id)
        servers_to_deploy.append(server)
    else:
        servers_to_deploy = Server.query.all()

    for server in servers_to_deploy:
        conn_addr = server.hostname

        wlogger.log(tid, "Setting up replication on server %s" % server.hostname, 'head')


        # 1. Ensure that server id is valid
        if not server:
            wlogger.log(tid, "Server is not on database", "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return False

        # determine chroot
        if not server.gluu_server:
            chroot = '/'
        else:
            chroot = '/opt/gluu-server-' + app_config.gluu_version

        # 2. Make SSH Connection to the remote server
        wlogger.log(tid, "Making SSH connection to the server %s" %
                    server.hostname)
        c = RemoteClient(server.hostname, ip=server.ip)
        try:
            c.startup()
        except Exception as e:
            wlogger.log(
                tid, "Cannot establish SSH connection {0}".format(e), "warning")
            wlogger.log(tid, "Ending server setup process.", "error")
            return False


        # 3. For Gluu server, ensure that chroot directory is available
        if server.gluu_server:
            if c.exists(chroot):
                wlogger.log(tid, 'Checking if remote is gluu server', 'success')
            else:
                wlogger.log(tid, "Remote is not a gluu server.", "error")
                wlogger.log(tid, "Ending server setup process.", "error")
                return False

        # 3.1 Ensure the data directories are available
        accesslog_dir = '/opt/gluu/data/accesslog'
        if not c.exists(chroot + accesslog_dir):
            run_command(tid, c, "mkdir -p {0}".format(accesslog_dir), chroot)
            run_command(tid, c, "chown -R ldap:ldap {0}".format(accesslog_dir),
                        chroot)

        # 4. Ensure Openldap is installed on the server
        if c.exists(os.path.join(chroot, 'opt/symas/bin/slaptest')):
            wlogger.log(tid, "Checking OpenLDAP is installed", "success")
        else:
            wlogger.log(tid, "Cannot find directory /opt/symas/bin. OpenLDAP is "
                             "not installed. Cannot setup replication.", "error")
            return False

        # 5. Upload symas-openldap.conf with remote access and slapd.d enabled
        syconf = os.path.join(chroot, 'opt/symas/etc/openldap/symas-openldap.conf')

        #symas-openldap.conf template filename
        confile = os.path.join(app.root_path, "templates", "slapd",
                               "symas-openldap.conf")

        ldap_bind_addr = server.hostname
        if app_config.use_ip:
            ldap_bind_addr = server.ip


        # prepare valus dictinory to be used for updating symas-openldap.conf
        # template. This file will make ldapserver listen outbond interface and
        # make ldapserver to run with olc
        values = dict(
            hosts="ldaps://127.0.0.1:1636/ ldaps://{0}:1636/".format(
                ldap_bind_addr),
            extra_args="-F /opt/symas/etc/openldap/slapd.d"
        )

        #read and update symas-openldap.conf file
        confile_content = open(confile).read()
        confile_content = confile_content.format(**values)

        #write symas-openldap.conf to server
        r = c.put_file(syconf, confile_content)

        if r[0]:
            wlogger.log(tid, 'symas-openldap.conf file uploaded', 'success')
        else:
            wlogger.log(tid, 'An error occured while uploading symas-openldap.conf'
                        ': {0}'.format(r[1]), "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return

        # 6. Generate OLC slapd.d
        wlogger.log(tid, "Convert slapd.conf to slapd.d OLC")

        # we need to stop ldapserver (solserver) to make it run with olc
        if server.os == 'CentOS 7' or server.os == 'RHEL 7':
            run_command(tid, c, "ssh -o IdentityFile=/etc/gluu/keys/gluu-console -o Port=60022 -o LogLevel=QUIET -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=yes root@localhost 'service solserver stop'")
        else:
            run_command(tid, c, 'service solserver stop', chroot)

        # remove slapd.d directory if it previously exist
        run_command(tid, c, "rm -rf /opt/symas/etc/openldap/slapd.d", chroot)
        # make slapd.d directory
        run_command(tid, c, "mkdir -p /opt/symas/etc/openldap/slapd.d", chroot)
        # convert convert slapd.conf to slapd.d
        run_command(tid, c, "/opt/symas/bin/slaptest -f /opt/symas/etc/openldap/"
                    "slapd.conf -F /opt/symas/etc/openldap/slapd.d", chroot)
        # make owner of slapd.d to ldap
        run_command(tid, c,
                    "chown -R ldap:ldap /opt/symas/etc/openldap/slapd.d", chroot)

        # 7. Restart the solserver with the new OLC configuration
        wlogger.log(tid, "Restarting LDAP server with OLC configuration")

        if server.os == 'CentOS 7' or server.os == 'RHEL 7':
            log= run_command(tid, c, "ssh -o IdentityFile=/etc/gluu/keys/gluu-console -o Port=60022 -o LogLevel=QUIET -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=yes root@localhost 'service solserver start'")
        else:
            log = run_command(tid, c, "service solserver start", chroot)
        if 'failed' in log:
            wlogger.log(tid, "Couldn't restart solserver.", "error")
            wlogger.log(tid, "Ending server setup process.", "error")

            if 'CentOS' in server.os or 'RHEL' in server.os:
                run_command(tid, c, "ssh -o IdentityFile=/etc/gluu/keys/gluu-console -o Port=60022 -o LogLevel=QUIET -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=yes root@localhost 'service solserver start -d 1'")
            else:
                run_command(tid, c, "service solserver start -d 1", chroot)
            return

        # 8. Connect to the OLC config
        ldp = LdapOLC('ldaps://{}:1636'.format(conn_addr), 'cn=config',
                      server.ldap_password)
        try:
            ldp.connect()
            wlogger.log(tid, 'Successfully connected to LDAPServer ', 'success')
        except Exception as e:
            wlogger.log(tid, "Connection to LDAPserver {0} at port 1636 was failed:"
                        " {1}".format(conn_addr, e), "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return

        # 9. Set the server ID
        if ldp.setServerID(server.id):
            wlogger.log(tid, 'Setting Server ID: {0}'.format(server.id), 'success')
        else:
            wlogger.log(tid, "Stting Server ID failed: {0}".format(
                ldp.conn.result['description']), "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return

        # 10. Enable the syncprov and load accesslog modules
        # load "syncprov", "accesslog" modules
        r = ldp.loadModules("syncprov", "accesslog")
        if r == -1:
            wlogger.log(
                tid, 'Syncprov and accesslog modlues already exist', 'debug')
        else:
            if r:
                wlogger.log(
                    tid, 'Syncprov and accesslog modlues were loaded', 'success')
            else:
                wlogger.log(tid, "Loading syncprov & accesslog failed: {0}".format(
                    ldp.conn.result['description']), "error")
                wlogger.log(tid, "Ending server setup process.", "error")
                return
        # if cccesslogDB entry not exists, create it
        if not ldp.checkAccesslogDBEntry():
            if ldp.accesslogDBEntry(app_config.replication_dn, accesslog_dir):
                wlogger.log(tid, 'Creating accesslog entry', 'success')
            else:
                wlogger.log(tid, "Creating accesslog entry failed: {0}".format(
                    ldp.conn.result['description']), "error")
                wlogger.log(tid, "Ending server setup process.", "error")
                return
        else:
            wlogger.log(tid, 'Accesslog entry already exists.', 'debug')

        # !WARNING UNBIND NECASSARY - I DON'T KNOW WHY.*****
        ldp.conn.unbind()
        ldp.conn.bind()

        #if not SyncprovOverlays exists on first database, create it
        if not ldp.checkSyncprovOverlaysDB1():
            if ldp.syncprovOverlaysDB1():
                wlogger.log(
                    tid, 'SyncprovOverlays entry on main database was created',
                    'success')
            else:
                wlogger.log(
                    tid, "Creating SyncprovOverlays entry on main database failed:"
                    " {0}".format(ldp.conn.result['description']), "error")
                wlogger.log(tid, "Ending server setup process.", "error")
                return
        else:
            wlogger.log(
                tid, 'SyncprovOverlays entry on main database already exists.',
                'debug')
        #if not SyncprovOverlays exists on second database, create it
        if not ldp.checkSyncprovOverlaysDB2():
            if ldp.syncprovOverlaysDB2():
                wlogger.log(
                    tid, 'SyncprovOverlay entry on accasslog database was created',
                    'success')
            else:
                wlogger.log(
                    tid, "Creating SyncprovOverlays entry on accasslog database"
                    " failed: {0}".format(ldp.conn.result['description']), "error")
                wlogger.log(tid, "Ending server setup process.", "error")
                return
        else:
            wlogger.log(
                tid, 'SyncprovOverlay entry on accasslog database already exists.',
                'debug')
        #if not accesslog purge entry exists on second database, create it
        if not ldp.checkAccesslogPurge():
            if ldp.accesslogPurge(app_config.log_purge):
                wlogger.log(tid, 'Creating accesslog purge entry', 'success')
            else:
                wlogger.log(tid, "Creating accesslog purge entry failed: {0}".format(
                    ldp.conn.result['description']), "warning")

        else:
            wlogger.log(tid, 'Accesslog purge entry already exists.', 'debug')

        #if not limits exists on main database, create it
        if ldp.setLimitOnMainDb(app_config.replication_dn):
            wlogger.log(
                tid, 'Setting size limit on main database for replicator user',
                'success')
        else:
            wlogger.log(tid, "Setting size limit on main database for replicator"
                        " user failed: {0}".format(ldp.conn.result['description']),
                        "warning")

        #replication user (dn) will be created only on primary server,
        #others will replicate it.
        if server.primary_server:
            # 11. Add replication user to the o=gluu
            wlogger.log(tid, 'Creating replicator user: {0}'.format(
                app_config.replication_dn))

            adminOlc = LdapOLC('ldaps://{}:1636'.format(conn_addr),
                               'cn=directory manager,o=gluu', server.ldap_password)
            try:
                adminOlc.connect()
            except Exception as e:
                wlogger.log(
                    tid, "Connection to LDAPserver as directory manager at port 1636"
                    " has failed: {0}".format(e), "error")
                wlogger.log(tid, "Ending server setup process.", "error")
                return

            if adminOlc.addReplicatorUser(app_config.replication_dn,
                                          app_config.replication_pw):
                wlogger.log(tid, 'Replicator user created.', 'success')
            else:
                wlogger.log(tid, "Creating replicator user failed: {0}".format(
                    adminOlc.conn.result), "warning")
                wlogger.log(tid, "Ending server setup process.", "error")
                return

        #If admin sets "use ip for replication, we will use ip address of server
        saddr = server.ip if app_config.use_ip else server.hostname


        # Prepare pDict for modifying ox-ldap.properties file.
        allproviders = Server.query.all()
        pDict = {}
        oxIDP=['localhost:1636']

        # we need to modify ox-ldap.properties on each server
        for ri in allproviders:

            laddr = ri.ip if app_config.use_ip else ri.hostname
            oxIDP.append(laddr+':1636')

            ox_auth = [ laddr+':1636' ]

            for rj in  allproviders:
                if not ri == rj:
                    laddr = rj.ip if app_config.use_ip else rj.hostname
                    ox_auth.append(laddr+':1636')

            pDict[ri.hostname]= ','.join(ox_auth)

        #If this is primary server, we need to modify OxIDPAuthentication entry
        #to include all servers in cluster. Others will replicate this.
        if server.primary_server:
            if adminOlc.configureOxIDPAuthentication(oxIDP):
                wlogger.log(tid, 'oxIDPAuthentication entry is modified to include all privders','success')
            else:
                wlogger.log(tid, 'Modifying oxIDPAuthentication entry is failed: {}'.format(
                        adminOlc.conn.result['description']), 'success')

        modifyOxLdapProperties(server, c, tid, pDict, chroot)

        #we need to restart gluu server after modifying oxIDPAuthentication entry
        if server.os == 'CentOS 7' or server.os == 'RHEL 7':
            restart_gluu_cmd = '/sbin/gluu-serverd-{0} restart'.format(app_config.gluu_version)
        else:
            restart_gluu_cmd = 'service gluu-server-{0} restart'.format(app_config.gluu_version)

        # 12. Make this server to listen to all other providers
        providers = Server.query.filter(Server.id.isnot(server.id)).filter().all()

        if providers:
            wlogger.log(tid, "Adding Syncrepl to integrate the server in cluster")

        for p in providers:

            paddr = p.ip if app_config.use_ip else p.hostname

            if not server.primary_server:

                status = ldp.add_provider(
                    p.id, "ldaps://{0}:1636".format(paddr), app_config.replication_dn,
                    app_config.replication_pw)
                if status:
                    wlogger.log(tid, '>> Making LDAP of {0} listen to {1}'.format(
                        server.hostname, p.hostname), 'success')
                else:
                    wlogger.log(tid, '>> Making {0} listen to {1} failed: {2}'.format(
                        p.hostname, server.hostname, ldp.conn.result['description']),
                        "warning")


                pc = RemoteClient(p.hostname, ip=p.ip)
                try:
                    pc.startup()
                except:
                    pc = None
                    wlogger.log(tid, "Can't establish SSH connection to provider server: ".format(p.hostname), 'fail')
                    #wlogger.log(tid, "Ending server installation process.", "error")

                    return

                modifyOxLdapProperties(p, pc, tid, pDict, chroot)


                if not server_id == 'all':
                    wlogger.log(tid, 'Restarting Gluu Server on provider {0}'.format(p.hostname))
                    wlogger.log(tid, "SSH connection to provider server: {0}".format(p.hostname), 'success')

                    if pc:
                        run_command(tid, pc, restart_gluu_cmd, no_error='debug')

                pc.close()

        #If this is not primary server, we need it to run in mirror mode.
        if not server.primary_server:
            # 15. Enable Mirrormode in the server
            if providers:
                if not ldp.checkMirroMode():
                    if ldp.makeMirroMode():
                        wlogger.log(tid, 'Enabling mirror mode', 'success')
                    else:
                        wlogger.log(tid, "Enabling mirror mode failed: {0}".format(
                            ldp.conn.result['description']), "warning")
                else:
                    wlogger.log(tid, 'LDAP Server is already in mirror mode', 'debug')



        if not server_id == 'all':
            wlogger.log(tid, 'Restarting Gluu Server on this server: {0}'.format(server.hostname))
            run_command(tid, c, restart_gluu_cmd, no_error='debug')

        # 16. Set the mmr flag to True to indicate it has been configured
        server.mmr = True
        db.session.commit()
        c.close()

    #Restarting all gluu servers
    if server_id == 'all':
        for server in servers_to_deploy:
            c = RemoteClient(server.hostname, ip=server.ip)
            c.startup()
            wlogger.log(tid, 'Restarting Gluu Server: ' + server.hostname)
            run_command(tid, c, restart_gluu_cmd, no_error='debug')
            c.close()

        #Adding providers for primary server
        pproviders = Server.query.filter(
                                    Server.primary_server.isnot(True)
                                ).all()

        primary = Server.query.filter(Server.primary_server==True).first()

        ldp_primary = LdapOLC('ldaps://{}:1636'.format(primary.hostname),
                                'cn=config', server.ldap_password)
        ldp_primary.connect()

        for provider in pproviders:
            paddr = provider.ip if app_config.use_ip else provider.hostname
            status = ldp_primary.add_provider(
                        provider.id, "ldaps://{0}:1636".format(paddr),
                        app_config.replication_dn,
                        app_config.replication_pw
                    )
            if status:
                wlogger.log(tid, '>> Making LDAP of {0} listen to {1}'.format(
                        primary.hostname, provider.hostname), 'success')
            else:
                wlogger.log(tid, '>> Making {0} listen to {1} failed: {2}'.format(
                        primary.hostname, provider.hostname,
                        ldp_primary.conn.result['description']), "warning")

        if pproviders:
            if not ldp_primary.checkMirroMode():
                if ldp_primary.makeMirroMode():
                    wlogger.log(tid, 'Enabling mirror mode', 'success')
                else:
                    wlogger.log(tid, "Enabling mirror mode failed: {0}".format(
                        ldp_primary.conn.result['description']), "warning")
            else:
                wlogger.log(tid, 'LDAP Server is already in mirror mode', 'debug')

        ldp_primary.close()


    wlogger.log(tid, "Deployment is successful")



def get_os_type(c):

    # 2. Linux Distribution of the server
    cin, cout, cerr = c.run("ls /etc/*release")
    files = cout.split()
    cin, cout, cerr = c.run("cat "+files[0])

    if "Ubuntu" in cout and "14.04" in cout:
        return "Ubuntu 14"
    if "Ubuntu" in cout and "16.04" in cout:
        return "Ubuntu 16"
    if "CentOS" in cout and "release 6." in cout:
        return "CentOS 6"
    if "CentOS" in cout and "release 7." in cout:
        return "CentOS 7"
    if 'Red Hat Enterprise Linux' in cout and '7.':
        return 'RHEL 7'
    if 'Debian' in cout and "(jessie)" in cout:
        return 'Debian 8'
    if 'Debian' in cout and "(stretch)" in cout:
        return 'Debian 9'


def check_gluu_installation(c):
    """Checks if gluu server is installed

    Args:
        c (:object:`clustermgr.core.remote.RemoteClient`): client to be used
            for the SSH communication
    """
    appconf = AppConfiguration.query.first()
    check_file = ('/opt/gluu-server-{}/install/community-edition-setup/'
                  'setup.properties.last').format(
                                                appconf.gluu_version
                                            )
    return c.exists(check_file)


@celery.task
def collect_server_details(server_id):
    server = Server.query.get(server_id)
    appconf = AppConfiguration.query.first()
    c = RemoteClient(server.hostname, ip=server.ip)
    try:
        c.startup()
    except:
        return

    # 0. Make sure it is a Gluu Server
    chdir = "/opt/gluu-server-" + appconf.gluu_version
    if not c.exists(chdir):
        server.gluu_server = False
        chdir = '/'

    # 1. The components installed in the server
    components = {
        'oxAuth': 'opt/gluu/jetty/oxauth',
        'oxTrust': 'opt/gluu/jetty/identity',
        'OpenLDAP': 'opt/symas/etc/openldap',
        'Shibboleth': 'opt/shibboleth-idp',
        'oxAuthRP': 'opt/gluu/jetty/oxauth-rp',
        'Asimba': 'opt/gluu/jetty/asimba',
        'Passport': 'opt/gluu/node/passport',
    }
    installed = []
    for component, marker in components.iteritems():
        marker = os.path.join(chdir, marker)
        if c.exists(marker):
            installed.append(component)
    server.components = ",".join(installed)

    server.os = get_os_type(c)
    server.gluu_server = check_gluu_installation(c)

    db.session.commit()


def import_key(suffix, hostname, gluu_version, tid, c, sos):
    """Imports key for identity server

    Args:
        suffix (string): suffix of the key to be imported
        hostname (string): hostname of server
        gluu_version (string): version of installed gluu server
        tid (string): id of the task running the command

        c (:object:`clustermgr.core.remote.RemoteClient`): client to be used
            for the SSH communication
        sos: how to specify logger type
    """
    defaultTrustStorePW = 'changeit'
    defaultTrustStoreFN = '/opt/jre/jre/lib/security/cacerts'
    certFolder = '/etc/certs'
    public_certificate = '%s/%s.crt' % (certFolder, suffix)
    cmd =' '.join([
                    '/opt/jre/bin/keytool', "-import", "-trustcacerts",
                    "-alias", "%s_%s" % (hostname, suffix),
                    "-file", public_certificate, "-keystore",
                    defaultTrustStoreFN,
                    "-storepass", defaultTrustStorePW, "-noprompt"
                    ])

    chroot = '/opt/gluu-server-{0}'.format(gluu_version)

    if 'CentOS' in sos:
        command = "ssh -o IdentityFile=/etc/gluu/keys/gluu-console -o Port=60022 -o LogLevel=QUIET -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=yes root@localhost '{0}'".format(cmd)
    else:
        command = 'chroot {0} /bin/bash -c "{1}"'.format(chroot,
                                                         cmd)

    cin, cout, cerr = c.run(command)
    wlogger.log(tid, cmd, 'debug')
    wlogger.log(tid, cout+cerr, 'debug')


def delete_key(suffix, hostname, gluu_version, tid, c, sos):
    """Delted key of identity server

    Args:
        suffix (string): suffix of the key to be imported
        hostname (string): hostname of server
        gluu_version (string): version of installed gluu server
        tid (string): id of the task running the command

        c (:object:`clustermgr.core.remote.RemoteClient`): client to be used
            for the SSH communication
        sos: how to specify logger type
    """
    defaultTrustStorePW = 'changeit'
    defaultTrustStoreFN = '/opt/jre/jre/lib/security/cacerts'
    chroot = '/opt/gluu-server-{0}'.format(gluu_version)
    cert = "etc/certs/%s.crt" % (suffix)
    if c.exists(os.path.join(chroot, cert)):
        cmd=' '.join([
                        '/opt/jre/bin/keytool', "-delete", "-alias",
                        "%s_%s" % (hostname, suffix),
                        "-keystore", defaultTrustStoreFN,
                        "-storepass", defaultTrustStorePW
                        ])

        if 'CentOS' in sos:
            command = "ssh -o IdentityFile=/etc/gluu/keys/gluu-console -o Port=60022 -o LogLevel=QUIET -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=yes root@localhost '{0}'".format(cmd)
        else:
            command = 'chroot {0} /bin/bash -c "{1}"'.format(chroot,
                                                         cmd)
        cin, cout, cerr = c.run(command)
        wlogger.log(tid, cmd, 'debug')
        wlogger.log(tid, cout+cerr, 'debug')



@celery.task(bind=True)
def installGluuServer(self, server_id):
    """Install Gluu server

    Args:
        server_id: id of server to be installed
    """
    tid = self.request.id
    server = Server.query.get(server_id)
    pserver = Server.query.filter_by(primary_server=True).first()

    appconf = AppConfiguration.query.first()

    c = RemoteClient(server.hostname, ip=server.ip)

    #setup properties file path
    setup_properties_file = os.path.join(Config.DATA_DIR, 'setup.properties')

    gluu_server = 'gluu-server-' + appconf.gluu_version

    #If os type of this server was not idientified, return to home
    if not server.os:
        wlogger.log(tid, "OS type has not been identified.", 'fail')
        wlogger.log(tid, "Ending server installation process.", "error")
        return

    #If this is not primary server, we will download setup.properties file from
    #primary server
    if not server.primary_server:
        wlogger.log(tid, "Check if Primary Server is Installed")

        pc = RemoteClient(pserver.hostname, ip=pserver.ip)

        try:
            pc.startup()
        except:
            wlogger.log(tid, "Can't make SSH connection to "
                             "primary server: ".format(
                             pserver.hostname), 'error')
            wlogger.log(tid, "Ending server installation process.", "error")
            return

        if check_gluu_installation(pc):
            wlogger.log(tid, "Primary Server is Installed",'success')
        else:
            wlogger.log(tid, "Primary Server is not Installed. "
                             "Please first install Primary Server",'fail')
            wlogger.log(tid, "Ending server installation process.", "error")
            return

    try:
        c.startup()
    except:
        wlogger.log(tid, "Can't establish SSH connection",'fail')
        wlogger.log(tid, "Ending server installation process.", "error")
        return

    wlogger.log(tid, "Preparing for Installation")

    start_command  = 'service gluu-server-{0} start'
    stop_command   = 'service gluu-server-{0} stop'
    enable_command = None


    #add gluu server repo and imports signatures
    if ('Ubuntu' in server.os) or ('Debian' in server.os):

        if server.os == 'Ubuntu 14':
            dist = 'trusty'
        elif server.os == 'Ubuntu 16':
            dist = 'xenial'


        if 'Ubuntu' in server.os:
            cmd = 'curl https://repo.gluu.org/ubuntu/gluu-apt.key | apt-key add -'
        elif 'Debian' in server.os:
            cmd = 'curl https://repo.gluu.org/debian/gluu-apt.key | apt-key add -'

        run_command(tid, c, cmd, no_error='debug')

        if 'Ubuntu' in server.os:
            cmd = ('echo "deb https://repo.gluu.org/ubuntu/ {0} main" '
               '> /etc/apt/sources.list.d/gluu-repo.list'.format(dist))
        elif 'Debian' in server.os:
            cmd = ('echo "deb https://repo.gluu.org/debian/ stable main" '
               '> /etc/apt/sources.list.d/gluu-repo.list')

        run_command(tid, c, cmd)

        install_command = 'DEBIAN_FRONTEND=noninteractive apt-get '

        cmd = 'DEBIAN_FRONTEND=noninteractive apt-get update'
        wlogger.log(tid, cmd, 'debug')
        cin, cout, cerr = c.run(cmd)
        wlogger.log(tid, cout+'\n'+cerr, 'debug')

        if 'dpkg --configure -a' in cerr:
            cmd = 'dpkg --configure -a'
            wlogger.log(tid, cmd, 'debug')
            cin, cout, cerr = c.run(cmd)
            wlogger.log(tid, cout+'\n'+cerr, 'debug')


    elif 'CentOS' in server.os or 'RHEL' in server.os:
        install_command = 'yum '
        if server.os == 'CentOS 7' or server.os == 'RHEL 7':
            enable_command  = '/sbin/gluu-serverd-{0} enable'
            stop_command    = '/sbin/gluu-serverd-{0} stop'
            start_command   = '/sbin/gluu-serverd-{0} start'

        qury_package    = 'yum list installed | grep gluu-server-'

        if not c.exists('/usr/bin/wget'):
            cmd = install_command +'install -y wget'
            run_command(tid, c, cmd, no_error='debug')

        if server.os == 'CentOS 6':
            cmd = 'wget https://repo.gluu.org/centos/Gluu-centos6.repo -O /etc/yum.repos.d/Gluu.repo'
        elif server.os == 'CentOS 7':
            cmd = 'wget https://repo.gluu.org/centos/Gluu-centos7.repo -O /etc/yum.repos.d/Gluu.repo'
        elif server.os == 'RHEL 7':
            cmd = 'wget https://repo.gluu.org/rhel/Gluu-rhel7.repo -O /etc/yum.repos.d/Gluu.repo'

        run_command(tid, c, cmd, no_error='debug')

        cmd = 'wget https://repo.gluu.org/centos/RPM-GPG-KEY-GLUU -O /etc/pki/rpm-gpg/RPM-GPG-KEY-GLUU'
        run_command(tid, c, cmd, no_error='debug')

        cmd = 'rpm --import /etc/pki/rpm-gpg/RPM-GPG-KEY-GLUU'
        run_command(tid, c, cmd, no_error='debug')

        cmd = 'yum clean all'
        run_command(tid, c, cmd, no_error='debug')

    wlogger.log(tid, "Check if Gluu Server was installed")



    gluu_installed = False

    #Determine if a version of gluu server was installed.
    r = c.listdir("/opt")
    if r[0]:
        for s in r[1]:
            m=re.search("gluu-server-(?P<gluu_version>(\d+).(\d+).(\d+))$",s)
            if m:
                gluu_version = m.group("gluu_version")
                gluu_installed = True
                cmd = stop_command.format(gluu_version)
                rs = run_command(tid, c, cmd, no_error='debug')

                #If gluu server is installed, first stop it then remove
                if "Can't stop gluu server" in rs:
                    cmd = 'rm -f /var/run/{0}.pid'.format(gluu_server)
                    run_command(tid, c, cmd, no_error='debug')

                    cmd = "df -aP | grep %s | awk '{print $6}' | xargs -I {} umount -l {}" % (gluu_server)
                    run_command(tid, c, cmd, no_error='debug')

                    cmd = stop_command.format(gluu_version)
                    rs = run_command(tid, c, cmd, no_error='debug')

                run_command(tid, c, install_command + "remove -y "+s)


    if not gluu_installed:
        wlogger.log(tid, "Gluu Server was not previously installed", "debug")


    #start installing gluu server
    wlogger.log(tid, "Installing Gluu Server: " + gluu_server)

    cmd = install_command + 'install -y ' + gluu_server
    wlogger.log(tid, cmd, "debug")
    cin, cout, cerr = c.run(install_command + 'install -y ' + gluu_server)
    wlogger.log(tid, cout+cerr, "debug")

    #If previous installation was broken, make a re-installation. This sometimes
    #occur on ubuntu installations
    if 'half-installed' in cout + cerr:
        if ('Ubuntu' in server.os) or  ('Debian' in server.os):
            cmd = 'DEBIAN_FRONTEND=noninteractive  apt-get install --reinstall -y '+ gluu_server
            run_command(tid, c, cmd, no_error='debug')


    if enable_command:
        run_command(tid, c, enable_command.format(appconf.gluu_version), no_error='debug')

    run_command(tid, c, start_command.format(appconf.gluu_version))

    #Since we will make ssh inot centos container, we need to wait ssh server to
    #be started properly
    if server.os == 'CentOS 7' or server.os == 'RHEL 7':
        wlogger.log(tid, "Sleeping 10 secs to wait for gluu server start properly.")
        time.sleep(10)

    # If this server is primary, upload local setup.properties to server
    if server.primary_server:
        wlogger.log(tid, "Uploading setup.properties")
        r = c.upload(setup_properties_file, '/opt/{}/install/community-edition-setup/setup.properties'.format(gluu_server))
    # If this server is not primary, get setup.properties.last from primary
    # server and upload to this server
    else:
        #this is not primary server, so download setup.properties.last
        #from primary server and upload to this server
        pc = RemoteClient(pserver.hostname, ip=pserver.ip)
        try:
            pc.startup()
        except:
            wlogger.log(tid, "Can't establish SSH connection to primary server: ".format(pserver.hostname), 'error')
            wlogger.log(tid, "Ending server installation process.", "error")
            return

        # ldap_paswwrod of this server should be the same with primary server
        ldap_passwd = None


        remote_file = '/opt/{}/install/community-edition-setup/setup.properties.last'.format(gluu_server)
        wlogger.log(tid, 'Downloading setup.properties.last from primary server', 'debug')

       #get setup.properties.last from primary server.
        r=pc.get_file(remote_file)
        if r[0]:
            new_setup_properties=''
            setup_properties = r[1].readlines()
            #replace ip with address of this server
            for l in setup_properties:
                if l.startswith('ip='):
                    l = 'ip={0}\n'.format(server.ip)
                elif l.startswith('ldapPass='):
                    ldap_passwd = l.split('=')[1].strip()
                new_setup_properties += l

            #put setup.properties to server
            remote_file_new = '/opt/{}/install/community-edition-setup/setup.properties'.format(gluu_server)
            wlogger.log(tid, 'Uploading setup.properties', 'debug')
            c.put_file(remote_file_new,  new_setup_properties)

            if ldap_passwd:
                server.ldap_password = ldap_passwd
        else:
            wlogger.log(tid, "Can't download setup.properties.last from primary server", 'fail')
            wlogger.log(tid, "Ending server installation process.", "error")
            return

    #run setup.py on the server
    wlogger.log(tid, "Running setup.py - Be patient this process will take a while ...")

    if server.os == 'CentOS 7' or server.os == 'RHEL 7':
        cmd = "ssh -o IdentityFile=/etc/gluu/keys/gluu-console -o Port=60022 -o LogLevel=QUIET -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=yes root@localhost 'cd /install/community-edition-setup/ && ./setup.py -n'"
        run_command(tid, c, cmd)
    else:
        cmd = 'cd /install/community-edition-setup/ && ./setup.py -n'
        run_command(tid, c, cmd, '/opt/'+gluu_server+'/', no_error='debug')


    setup_prop = get_setup_properties()

    # Get slapd.conf from primary server and upload this server
    if not server.primary_server:

        if setup_prop['ldap_type'] == 'openldap':
            #FIXME: Check this later
            cmd = 'rm /opt/gluu/data/main_db/*.mdb'
            run_command(tid, c, cmd, '/opt/'+gluu_server)


            slapd_conf_file = '/opt/{0}/opt/symas/etc/openldap/slapd.conf'.format(gluu_server)
            r = pc.get_file(slapd_conf_file)
            if r[0]:
                fc = r[1].read()
                r2 = c.put_file(slapd_conf_file, fc)
                if not r2[0]:
                    wlogger.log(tid, "Can't put slapd.conf to this server: ".format(r[1]), 'error')
                else:
                    wlogger.log(tid, "slapd.conf was downloaded from primary server and uploaded to this server", 'success')
            else:
                wlogger.log(tid, "Can't get slapd.conf from primary server: ".format(r[1]), 'error')


            #If primary server conatins any custom schema, get them and put to this server
            wlogger.log(tid, 'Downloading custom schema files from primary server and upload to this server')
            custom_schema_files = pc.listdir("/opt/{0}/opt/gluu/schema/openldap/".format(gluu_server))

            if custom_schema_files[0]:
                for csf in custom_schema_files[1]:
                    local = '/tmp/'+csf
                    remote = '/opt/{0}/opt/gluu/schema/openldap/{1}'.format(gluu_server, csf)

                    pc.download(remote, local)
                    c.upload(local, remote)
                    os.remove(local)
                    wlogger.log(tid, '{0} dowloaded from from primary and uploaded'.format(csf), 'debug')

            #stop and start solserver
            if server.os == 'CentOS 7' or server.os == 'RHEL 7':
                run_command(tid, c, "ssh -o IdentityFile=/etc/gluu/keys/gluu-console -o Port=60022 -o LogLevel=QUIET -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=yes root@localhost 'service solserver stop'")
            else:
                run_command(tid, c, 'service solserver stop', '/opt/'+gluu_server)

            if server.os == 'CentOS 7' or server.os == 'RHEL 7':
                run_command(tid, c, "ssh -o IdentityFile=/etc/gluu/keys/gluu-console -o Port=60022 -o LogLevel=QUIET -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=yes root@localhost 'service solserver start'")
            else:
                run_command(tid, c, 'service solserver start', '/opt/'+gluu_server)

        #If gluu version is greater than 3.0.2 we need to download certificates
        #from primary server and upload to this server, then will delete and
        #import keys
        if appconf.gluu_version > '3.0.2':
            wlogger.log(tid, "Downloading certificates from primary "
                             "server and uploading to this server")
            certs_remote_tmp = "/tmp/certs_"+str(uuid.uuid4())[:4].upper()+".tgz"
            certs_local_tmp = "/tmp/certs_"+str(uuid.uuid4())[:4].upper()+".tgz"

            cmd = 'tar -zcf {0} /opt/gluu-server-{1}/etc/certs/'.format(
                                certs_remote_tmp, appconf.gluu_version)
            wlogger.log(tid,cmd,'debug')
            cin, cout, cerr = pc.run(cmd)
            wlogger.log(tid, cout+cerr, 'debug')
            wlogger.log(tid,cmd,'debug')

            r = pc.download(certs_remote_tmp, certs_local_tmp)
            if 'Download successful' in r :
                wlogger.log(tid, r,'success')
            else:
                wlogger.log(tid, r,'error')

            r = c.upload(certs_local_tmp, "/tmp/certs.tgz")

            if 'Upload successful' in r:
                wlogger.log(tid, r,'success')
            else:
                wlogger.log(tid, r,'error')

            cmd = 'tar -zxf /tmp/certs.tgz -C /'
            run_command(tid, c, cmd)



            #delete old keys and import new ones
            wlogger.log(tid, 'Manuplating keys')
            for suffix in (
                    'httpd',
                    'shibIDP',
                    'idp-encryption',
                    'asimba',
                    setup_prop['ldap_type'],
                    ):
                delete_key(suffix, appconf.nginx_host, appconf.gluu_version,
                            tid, c, server.os)
                import_key(suffix, appconf.nginx_host, appconf.gluu_version,
                            tid, c, server.os)

    else:
        #this is primary server so we need to upload local custom schemas if any
        custom_schema_dir = os.path.join(Config.DATA_DIR, 'schema')
        custom_schemas = os.listdir(custom_schema_dir)

        if custom_schemas:
            for sf in custom_schemas:
                local = os.path.join(custom_schema_dir, sf)
                remote = '/opt/{0}/opt/gluu/schema/openldap/{1}'.format(
                    gluu_server, sf)
                r = c.upload(local, remote)
                if r[0]:
                    wlogger.log(tid, 'Custom schame file {0} uploaded'.format(
                            sf), 'success')
                else:
                    wlogger.log(tid,
                        "Can't upload custom schame file {0}: ".format(sf,
                                                                r[1]), 'error')

    #ntp is required for time sync, since ldap replication will be
    #done by time stamp. If not isntalled, install and configure crontab
    wlogger.log(tid, "Checking if ntp is installed and configured.")

    if c.exists('/usr/sbin/ntpdate'):
        wlogger.log(tid, "ntp was installed", 'success')
    else:

        cmd = install_command + 'install -y ntpdate'
        run_command(tid, c, cmd)

    #run time sync an every minute
    c.put_file('/etc/cron.d/setdate',
                '* * * * *    root    /usr/sbin/ntpdate -s time.nist.gov\n')
    wlogger.log(tid, 'Crontab entry was created to update time in every minute',
                     'debug')

    if 'CentOS' in server.os or 'RHEL' in server.os:
        cmd = 'service crond reload'
    else:
        cmd = 'service cron reload'

    run_command(tid, c, cmd, no_error='debug')

    server.gluu_server = True
    db.session.commit()
    wlogger.log(tid, "Gluu Server successfully installed")



@celery.task(bind=True)
def removeMultiMasterDeployement(self, server_id):
    """Removes multi master replication deployment

    Args:
        server_id: id of server to be un-depoloyed
    """
    app_config = AppConfiguration.query.first()
    server = Server.query.get(server_id)
    tid = self.request.id
    app_config = AppConfiguration.query.first()
    if not server.gluu_server:
        chroot = '/'
    else:
        chroot = '/opt/gluu-server-' + app_config.gluu_version

    wlogger.log(tid, "Making SSH connection to the server %s" %
                server.hostname)
    c = RemoteClient(server.hostname, ip=server.ip)

    try:
        c.startup()
    except Exception as e:
        wlogger.log(
            tid, "Cannot establish SSH connection {0}".format(e), "warning")
        wlogger.log(tid, "Ending server setup process.", "error")
        return False

    if server.gluu_server:
        # check if remote is gluu server
        if c.exists(chroot):
            wlogger.log(tid, 'Checking if remote is gluu server', 'success')
        else:
            wlogger.log(tid, "Remote is not a gluu server.", "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return False

    # symas-openldap.conf file exists
    if c.exists(os.path.join(chroot, 'opt/symas/etc/openldap/symas-openldap.conf')):
        wlogger.log(tid, 'Checking symas-openldap.conf exists', 'success')
    else:
        wlogger.log(tid, 'Checking if symas-openldap.conf exists', 'fail')
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    # sldapd.conf file exists
    if c.exists(os.path.join(chroot, 'opt/symas/etc/openldap/slapd.conf')):
        wlogger.log(tid, 'Checking slapd.conf exists', 'success')
    else:
        wlogger.log(tid, 'Checking if slapd.conf exists', 'fail')
        wlogger.log(tid, "Ending server setup process.", "error")
        return

    # uplodading symas-openldap.conf file
    confile = os.path.join(app.root_path, "templates",
                           "slapd", "symas-openldap.conf")
    confile_content = open(confile).read()

    # prepare vals dict to modify symas-openldap.conf template
    vals = dict(
        hosts='ldaps://127.0.0.1:1636/',
        extra_args='',
    )

    # modify symas-openldap.conf template
    confile_content = confile_content.format(**vals)

    # put to remote server
    r = c.put_file(os.path.join(
        chroot, 'opt/symas/etc/openldap/symas-openldap.conf'), confile_content)

    if r[0]:
        wlogger.log(tid, 'symas-openldap.conf file uploaded', 'success')
    else:
        wlogger.log(tid, 'An error occured while uploading symas-openldap.conf'
                    ': {0}'.format(r[1]), "error")
        wlogger.log(tid, "Ending server setup process.", "error")
        return
    #change owner
    run_command(tid, c, "chown -R ldap:ldap /opt/symas/etc/openldap", chroot)

    # remove slapd.d directory
    slapd_d_dir = os.path.join(chroot, 'opt/symas/etc/openldap/')
    if c.exists(slapd_d_dir):
        cmd = "rm -rf /opt/symas/etc/openldap/slapd.d"
        run_command(tid, c, cmd, chroot)


    server.mmr = False
    db.session.commit()

    #modifyOxLdapProperties(server, c, tid)

    # Restart the solserver with slapd.conf configuration
    wlogger.log(tid, "Restarting LDAP server with slapd.conf configuration")

    if 'CentOS' in server.os:
        run_command(tid, c, "ssh -o IdentityFile=/etc/gluu/keys/gluu-console -o Port=60022 -o LogLevel=QUIET -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=yes root@localhost 'service solserver restart'")
    else:
        log = run_command(tid, c, "service solserver restart", chroot)

        if 'failed' in log:
            wlogger.log(tid,
                        "There seems to be some issue in restarting the server.",
                        "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return

    wlogger.log(tid, 'Deployment of Ldap Server was successfully removed')

    return True



@celery.task(bind=True)
def opendjdisablereplication(self, server_id, remove_server=False):

    app_config = AppConfiguration.query.first()
    primary_server = Server.query.filter_by(primary_server=True).first()
    server = Server.query.get(server_id)
    tid = self.request.id

    c = RemoteClient(primary_server.hostname, ip=primary_server.ip)
    chroot = '/opt/gluu-server-' + app_config.gluu_version


    cmd_run = '{}'


    if (server.os == 'CentOS 7') or (server.os == 'RHEL 7'):
        chroot = None
        cmd_run = ('ssh -o IdentityFile=/etc/gluu/keys/gluu-console '
                '-o Port=60022 -o LogLevel=QUIET '
                '-o StrictHostKeyChecking=no '
                '-o UserKnownHostsFile=/dev/null '
                '-o PubkeyAuthentication=yes root@localhost "{}"')

    wlogger.log(tid, 
            "Making SSH connection to primary server {0}".format(
            primary_server.hostname)
            )

    try:
        c.startup()
    except Exception as e:
        wlogger.log(
            tid, "Cannot establish SSH connection {0}".format(e), "warning")
        wlogger.log(tid, "Ending server setup process.", "error")
        return False

    cmd = ('/opt/opendj/bin/dsreplication disable --disableAll --port 4444 '
            '--hostname {} --adminUID admin --adminPassword {} '
            '--trustAll --no-prompt').format(
                            server.hostname,
                            app_config.replication_pw)

    cmd = cmd_run.format(cmd)
    run_command(tid, c, cmd, chroot)

    wlogger.log(tid, "Checking replication status")

    cmd = ('/opt/opendj/bin/dsreplication status -n -X -h {} '
            '-p 1444 -I admin -w {}').format(
                    primary_server.hostname,
                    app_config.replication_pw)

    cmd = cmd_run.format(cmd)
    run_command(tid, c, cmd, chroot)

    server.mmr = False

    if remove_server:
        db.session.delete(server)

    db.session.commit()
    return True


@celery.task(bind=True)
def opendjenablereplication(self, server_id):

    app_config = AppConfiguration.query.first()

    primary_server = Server.query.filter_by(primary_server=True).first()
    tid = self.request.id
    app_config = AppConfiguration.query.first()


    if server_id == 'all':
        servers = Server.query.all()
    else:
        servers = [Server.query.get(server_id)]

    if not primary_server.gluu_server:
        chroot = '/'
    else:
        chroot = '/opt/gluu-server-' + app_config.gluu_version

    chroot_fs = '/opt/gluu-server-' + app_config.gluu_version

    wlogger.log(tid, "Making SSH connection to the primary server %s" %
                primary_server.hostname)

    c = RemoteClient(primary_server.hostname, ip=primary_server.ip)

    try:
        c.startup()
    except Exception as e:
        wlogger.log(
            tid, "Cannot establish SSH connection {0}".format(e), "warning")
        wlogger.log(tid, "Ending server setup process.", "error")
        return False

    if primary_server.gluu_server:
        # check if remote is gluu server
        if c.exists(chroot):
            wlogger.log(tid, 'Checking if remote is gluu server', 'success')
        else:
            wlogger.log(tid, "Remote is not a gluu server.", "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return False


    tmp_dir = os.path.join('/tmp', uuid.uuid1().hex[:12])
    os.mkdir(tmp_dir)

    wlogger.log(tid, "Downloading opendj certificates")

    opendj_cert_files = ('keystore', 'keystore.pin', 'truststore')

    for cf in opendj_cert_files:
        remote = os.path.join(chroot_fs, 'opt/opendj/config', cf)
        local = os.path.join(tmp_dir, cf)
        result = c.download(remote, local)
        if not result.startswith('Download successful'):
            wlogger.log(tid, result, "warning")
            wlogger.log(tid, "Ending server setup process.", "error")
            return False

    oxIDP=['localhost:1636']

    for server in servers:
        laddr = server.ip if app_config.use_ip else server.hostname
        oxIDP.append(laddr+':1636')

    adminOlc = LdapOLC('ldaps://{}:1636'.format(primary_server.hostname),
                        'cn=directory manager', primary_server.ldap_password)

    try:
        adminOlc.connect()
    except Exception as e:
        wlogger.log(
            tid, "Connection to LDAPserver as directory manager at port 1636"
            " has failed: {0}".format(e), "error")
        wlogger.log(tid, "Ending server setup process.", "error")
        return


    if adminOlc.configureOxIDPAuthentication(oxIDP):
        wlogger.log(tid,
                'oxIDPAuthentication entry is modified to include all privders',
                'success')
    else:
        wlogger.log(tid, 'Modifying oxIDPAuthentication entry is failed: {}'.format(
                adminOlc.conn.result['description']), 'success')

    primary_server_secured = False

    for server in servers:
        if not server.primary_server:
            wlogger.log(tid, "Enabling replication on server {}".format(
                                                            server.hostname))

            cmd_run = '{}'

            if (server.os == 'CentOS 7') or (server.os == 'RHEL 7'):
                chroot = None
                cmd_run = ('ssh -o IdentityFile=/etc/gluu/keys/gluu-console '
                        '-o Port=60022 -o LogLevel=QUIET '
                        '-o StrictHostKeyChecking=no '
                        '-o UserKnownHostsFile=/dev/null '
                        '-o PubkeyAuthentication=yes root@localhost "{}"')


            cmd = ('/opt/opendj/bin/dsreplication enable --host1 {} --port1 4444 '
                    '--bindDN1 \'cn=directory manager\' --bindPassword1 {} '
                    '--replicationPort1 8989 --host2 {} --port2 4444 --bindDN2 '
                    '\'cn=directory manager\' --bindPassword2 {} '
                    '--replicationPort2 8989 --adminUID admin --adminPassword {} '
                    '--baseDN \'o=gluu\' --trustAll -X -n').format(
                        primary_server.hostname,
                        primary_server.ldap_password,
                        server.hostname,
                        server.ldap_password,
                        app_config.replication_pw,
                        )

            cmd = cmd_run.format(cmd)
            run_command(tid, c, cmd, chroot)

            wlogger.log(tid, "Inıtializing replication on server {}".format(
                                                            server.hostname))


            cmd = ('/opt/opendj/bin/dsreplication initialize --baseDN \'o=gluu\' '
                    '--adminUID admin --adminPassword {} --hostSource {} '
                    '--portSource 4444  --hostDestination {} --portDestination 4444 '
                    '--trustAll -X -n').format(
                        app_config.replication_pw,
                        primary_server.hostname,
                        server.hostname,
                        )

            cmd = cmd_run.format(cmd)
            run_command(tid, c, cmd, chroot)

            if not primary_server_secured:

                wlogger.log(tid, "Securing replication on primary server {}".format(
                                                                primary_server.hostname))

                cmd = ('/opt/opendj/bin/dsconfig -h {} -p 4444 '
                        ' -D  \'cn=Directory Manager\' -w {} --trustAll '
                        '-n set-crypto-manager-prop --set ssl-encryption:true'
                        ).format(primary_server.hostname, primary_server.ldap_password)

                cmd = cmd_run.format(cmd)
                run_command(tid, c, cmd, chroot)
                primary_server_secured = True
                primary_server.mmr = True

            wlogger.log(tid, "Securing replication on server {}".format(
                                                            server.hostname))
            cmd = ('/opt/opendj/bin/dsconfig -h {} -p 4444 '
                    ' -D  \'cn=Directory Manager\' -w {} --trustAll '
                    '-n set-crypto-manager-prop --set ssl-encryption:true'
                    ).format(server.hostname, primary_server.ldap_password)

            cmd = cmd_run.format(cmd)
            run_command(tid, c, cmd, chroot)

            server.mmr = True


    db.session.commit()



    servers = Server.query.all()

    pDict = {}
    for server in servers:
        if server.mmr:
            laddr = server.ip if app_config.use_ip else server.hostname
            ox_auth = [ laddr+':1636' ]
            for prsrv in servers:
                if prsrv.mmr:
                    if not prsrv == server:
                        laddr = prsrv.ip if app_config.use_ip else prsrv.hostname
                        ox_auth.append(laddr+':1636')
            pDict[server.hostname]= ','.join(ox_auth)


    for server in servers:


        if server.os == 'CentOS 7' or server.os == 'RHEL 7':
            restart_command = '/sbin/gluu-serverd-{0} restart'.format(
                                app_config.gluu_version)
        else:
            restart_command = '/etc/init.d/gluu-server-{0} restart'.format(
                                app_config.gluu_version)

        if server.primary_server:
            modifyOxLdapProperties(server, c, tid, pDict, chroot_fs)

            wlogger.log(tid, "Restarting Gluu Server on {}".format(
                                server.hostname))

            run_command(tid, c, restart_command)

        else:

            wlogger.log(tid, "Making SSH connection to the server %s" %
                    server.hostname)

            ct = RemoteClient(server.hostname, ip=server.ip)

            try:
                ct.startup()
            except Exception as e:
                wlogger.log(
                    tid, "Cannot establish SSH connection {0}".format(e),
                    "warning")
                wlogger.log(tid, "Ending server setup process.", "error")
                return False

            modifyOxLdapProperties(server, ct, tid, pDict, chroot_fs)

            wlogger.log(tid, "Uploading OpenDj certificate files")
            for cf in opendj_cert_files:


                remote = os.path.join(chroot_fs, 'opt/opendj/config', cf)
                local = os.path.join(tmp_dir, cf)

                result = ct.upload(local, remote)
                if not result:
                    wlogger.log(tid, "An error occurred while uploading OpenDj certificates.", "error")
                    return False

                if not result.startswith('Upload successful'):
                    wlogger.log(tid, result, "warning")
                    wlogger.log(tid, "Ending server setup process.", "error")
                    return False

            wlogger.log(tid, "Restarting Gluu Server on {}".format(
                                server.hostname))

            run_command(tid, ct, restart_command)

            ct.close()

    if 'CentOS' in primary_server.os:
        wlogger.log(tid, "Waiting for Gluu to finish starting")
        time.sleep(10)
    

    wlogger.log(tid, "Checking replication status")

    cmd = ('/opt/opendj/bin/dsreplication status -n -X -h {} '
            '-p 1444 -I admin -w {}').format(
                    primary_server.hostname,
                    app_config.replication_pw)

    cmd = cmd_run.format(cmd)
    run_command(tid, c, cmd, chroot)

    c.close()

    return True


@celery.task(bind=True)
def installNGINX(self, nginx_host):
    """Installs nginx load balancer

    Args:
        nginx_host: hostname of server on which we will install nginx
    """
    tid = self.request.id
    app_config = AppConfiguration.query.first()
    pserver = Server.query.filter_by(primary_server=True).first()
    wlogger.log(tid, "Making SSH connection to the server {}".format(
                                                                nginx_host))
    c = RemoteClient(nginx_host)

    try:
        c.startup()
    except Exception as e:
        wlogger.log(
            tid, "Cannot establish SSH connection {0}".format(e), "warning")
        wlogger.log(tid, "Ending server setup process.", "error")
        return False

    # We should determine os type to install nginx
    wlogger.log(tid, "Determining OS type")
    os_type = get_os_type(c)
    wlogger.log(tid, "OS is determined as {0}".format(os_type),'debug')

    #check if nginx was installed on this server
    wlogger.log(tid, "Check if NGINX installed")

    r = c.exists("/usr/sbin/nginx")

    if r:
        wlogger.log(tid, "nginx allready exists")
    else:
        #If this is centos we need to install epel-release
        if 'CentOS' in os_type:
            run_command(tid, c, 'yum install -y epel-release')
            cmd = 'yum install -y nginx'
        else:
            run_command(tid, c, 'DEBIAN_FRONTEND=noninteractive apt-get update')
            cmd = 'DEBIAN_FRONTEND=noninteractive apt-get install -y nginx'

        wlogger.log(tid, cmd, 'debug')

        #FIXME: check cerr??
        cin, cout, cerr = c.run(cmd)
        wlogger.log(tid, cout, 'debug')

    #Check if ssl certificates directory exist on this server
    r = c.exists("/etc/nginx/ssl/")
    if not r:
        wlogger.log(tid, "/etc/nginx/ssl/ does not exists. Creating ...",
                            "debug")
        r2 = c.mkdir("/etc/nginx/ssl/")
        if r2[0]:
            wlogger.log(tid, "/etc/nginx/ssl/ was created", "success")
        else:
            wlogger.log(tid, "Error creating /etc/nginx/ssl/ {0}".format(r2[1]),
                            "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return False
    else:
        wlogger.log(tid, "Directory /etc/nginx/ssl/ exists.", "debug")

    # we need to download ssl certifiactes from primary server.
    wlogger.log(tid, "Making SSH connection to primary server {} for "
                     "downloading certificates".format(pserver.hostname))
    pc = RemoteClient(pserver.hostname, pserver.ip)
    try:
        pc.startup()
    except Exception as e:
        wlogger.log(
            tid, "Cannot establish SSH connection to primary server {0}".format(e), "warning")
        wlogger.log(tid, "Ending server setup process.", "error")
        return False
    # get httpd.crt and httpd.key from primary server and put to this server
    for crt in ('httpd.crt', 'httpd.key'):
        wlogger.log(tid, "Downloading {0} from primary server".format(crt), "debug")
        remote_file = '/opt/gluu-server-{0}/etc/certs/{1}'.format(app_config.gluu_version, crt)
        r = pc.get_file(remote_file)
        if not r[0]:
            wlogger.log(tid, "Can't download {0} from primary server: {1}".format(crt,r[1]), "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return False
        else:
            wlogger.log(tid, "File {} was downloaded.".format(remote_file), "success")
        fc = r[1].read()
        remote = os.path.join("/etc/nginx/ssl/", crt)
        r = c.put_file(remote, fc)

        if r[0]:
            wlogger.log(tid, "File {} uploaded".format(remote), "success")
        else:
            wlogger.log(tid, "Can't upload {0}: {1}".format(remote,r[1]), "error")
            wlogger.log(tid, "Ending server setup process.", "error")
            return False

    servers = Server.query.all()
    nginx_backends = []

    #read local nginx.conf template
    nginx_tmp_file = os.path.join(app.root_path, "templates", "nginx",
                           "nginx.temp")
    nginx_tmp = open(nginx_tmp_file).read()

    #add all gluu servers to nginx.conf
    for s in servers:
        nginx_backends.append('  server {0}:443 max_fails=2 fail_timeout=10s;'.format(s.hostname))

    nginx_tmp = nginx_tmp.replace('{#NGINX#}', nginx_host)
    nginx_tmp = nginx_tmp.replace('{#SERVERS#}', '\n'.join(nginx_backends))

    #put nginx.conf to server
    remote = "/etc/nginx/nginx.conf"
    r = c.put_file(remote, nginx_tmp)

    if r[0]:
         wlogger.log(tid, "File {} uploaded".format(remote), "success")
    else:
        wlogger.log(tid, "Can't upload {0}: {1}".format(remote,r[1]), "error")
        wlogger.log(tid, "Ending server setup process.", "error")
        return False
    #it is time to start nginx server
    cmd = 'service nginx restart'

    run_command(tid, c, cmd, no_error='debug')

    wlogger.log(tid, "NGINX successfully installed")
