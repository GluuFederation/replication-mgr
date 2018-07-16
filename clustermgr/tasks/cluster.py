# -*- coding: utf-8 -*-

import os
import re
import time
import subprocess

from flask import current_app as app

from clustermgr.models import Server, AppConfiguration
from clustermgr.extensions import wlogger, db, celery
from clustermgr.core.remote import RemoteClient
from clustermgr.core.ldap_functions import LdapOLC, getLdapConn
from clustermgr.core.utils import get_setup_properties, modify_etc_hosts, \
        make_nginx_proxy_conf, make_twem_proxy_conf, make_proxy_stunnel_conf
from clustermgr.core.clustermgr_installer import Installer
from clustermgr.config import Config
import uuid
import select

def run_command(tid, c, command, container=None, no_error='error',  server_id='', exclude_error=None):
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
    
    excluded_errors = [
                        'config file testing succeeded',
                        'There are no base DNs available to enable replication between the two servers',
                    ]
                    
    if exclude_error:
        excluded_errors.append(excluded_errors)
    
    if container == '/':
        container = None
    if container:
        command = 'chroot {0} /bin/bash -c "{1}"'.format(container,
                                                         command)

    wlogger.log(tid, command, "debug", server_id=server_id)


    cin, cout, cerr = c.run(command)
    output = ''
    if cout:
        wlogger.log(tid, cout, "debug", server_id=server_id)
        output += "\n" + cout
    if cerr:
        
        not_error = False
        for ee in excluded_errors:
            if ee in cerr:
                not_error = True
                break
        
        # For some reason slaptest decides to send success message as err, so
        if not_error:
            wlogger.log(tid, cerr, "debug", server_id=server_id)
        else:
            wlogger.log(tid, cerr, no_error, server_id=server_id)
        output += "\n" + cerr

    return output


def upload_file(tid, c, local, remote, server_id=''):
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
    wlogger.log(tid, out, 'error' if 'Error' in out else 'success', server_id=server_id)


def download_file(tid, c, remote, local, server_id=''):
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
    wlogger.log(tid, out, 'error' if 'Error' in out else 'success', server_id=server_id)


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
                'all replicating servers'.format(server.hostname),
                'success')
        else:
            temp = r[1]
    else:
        temp = ox_ldap[1]

    if temp:
        wlogger.log(tid,
                'ox-ldap.properties file on {0} was not modified to '
                'include all replicating servers: {1}'.format(server.hostname, temp),
                'warning')


def get_csync2_config(exclude=None):

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

    cysnc_hosts = []
    for server in all_servers:
        if not server.hostname == exclude:
            cysnc_hosts.append(('csync{}.gluu'.format(server.id), server.ip))

    for srv in cysnc_hosts:
        csync2_config.append('  host {};'.format(srv[0]))

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


    csync2_config.append('  backup-directory /var/backups/csync2;')
    csync2_config.append('  backup-generations 3;')

    csync2_config.append('\n  auto younger;\n')

    csync2_config.append('}')

    csync2_config = '\n'.join(csync2_config)

    return csync2_config


@celery.task(bind=True)
def setup_filesystem_replication(self):
    """Deploys File System replicaton
    """

    tid = self.request.id

    servers = Server.query.all()
    app_config = AppConfiguration.query.first()

    chroot = '/opt/gluu-server-' + app_config.gluu_version
    
    cysnc_hosts = []
    for server in servers:
        cysnc_hosts.append(('csync{}.gluu'.format(server.id), server.ip))

    server_counter = 0

    for server in servers:
        
        c = RemoteClient(server.hostname, ip=server.ip)
        c.startup()
        
        modify_hosts(tid, c, cysnc_hosts, chroot=chroot, server_id=server.id)

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
            run_command(tid, c, cmd, chroot, server_id=server.id)

            cmd = 'locale-gen en_US.UTF-8'
            run_command(tid, c, cmd, chroot, server_id=server.id)

            install_command = 'DEBIAN_FRONTEND=noninteractive apt-get'

            cmd = '{} update'.format(install_command)
            run_command(tid, c, cmd, chroot, server_id=server.id)

            cmd = '{} install -y apt-utils'.format(install_command)
            run_command(tid, c, cmd, chroot, no_error=None, server_id=server.id)


            cmd = '{} install -y csync2'.format(install_command)
            run_command(tid, c, cmd, chroot, server_id=server.id)


            cmd = 'apt-get install -y csync2'
            run_command(tid, c, cmd, chroot, server_id=server.id)

        elif 'CentOS' in server.os:


            cmd = run_cmd.format('yum install -y epel-release')
            run_command(tid, c, cmd, cmd_chroot, no_error=None, server_id=server.id)

            cmd = run_cmd.format('yum repolist')
            run_command(tid, c, cmd, cmd_chroot, no_error=None, server_id=server.id)

            if server.os == 'CentOS 7':
                csync_rpm = 'https://github.com/mbaser/gluu/raw/master/csync2-2.0-3.gluu.centos7.x86_64.rpm'
            if server.os == 'CentOS 6':
                csync_rpm = 'https://github.com/mbaser/gluu/raw/master/csync2-2.0-3.gluu.centos6.x86_64.rpm'

            cmd = run_cmd.format('yum install -y ' + csync_rpm)
            run_command(tid, c, cmd, cmd_chroot, no_error=None, server_id=server.id)

            cmd = run_cmd.format('service xinetd stop')
            run_command(tid, c, cmd, cmd_chroot, no_error=None, server_id=server.id)

        if server.os == 'CentOS 6':
            cmd = run_cmd.format('yum install -y crontabs')
            run_command(tid, c, cmd, cmd_chroot, no_error=None, server_id=server.id)

        cmd = run_cmd.format('rm -f /var/lib/csync2/*.db3')
        run_command(tid, c, cmd, cmd_chroot, no_error=None, server_id=server.id)

        cmd = run_cmd.format('rm -f /etc/csync2*')
        run_command(tid, c, cmd, cmd_chroot, no_error=None, server_id=server.id)


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
                wlogger.log(tid, cmd, 'debug', server_id=server.id)
                run_command(tid, c, cmd, cmd_chroot, no_error=None,  server_id=server.id)


            csync2_config = get_csync2_config()

            remote_file = os.path.join(chroot, 'etc', 'csync2.cfg')

            wlogger.log(tid, "Uploading csync2.cfg", 'debug', server_id=server.id)

            c.put_file(remote_file,  csync2_config)


        else:
            wlogger.log(tid, "Downloading csync2.cfg, csync2.key, "
                        "csync2_ssl_cert.csr, csync2_ssl_cert.pem, and"
                        "csync2_ssl_key.pem from primary server and uploading",
                        'debug', server_id=server.id)

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

            wlogger.log(tid, "Enabling csync2 via inetd", server_id=server.id)

            fc = []
            inet_conf_file = os.path.join(chroot, 'etc','inetd.conf')
            r,f=c.get_file(inet_conf_file)
            csync_line = 'csync2\tstream\ttcp\tnowait\troot\t/usr/sbin/csync2\tcsync2 -i -l -N csync{}.gluu\n'.format(server.id) 
            csync_line_exists = False
            for l in f:
                if l.startswith('csync2'):
                    l = csync_line
                    csync_line_exists = True
                fc.append(l)
            if not csync_line_exists:
                fc.append(csync_line)
            fc=''.join(fc)
            c.put_file(inet_conf_file, fc)

            cmd = '/etc/init.d/openbsd-inetd restart'
            run_command(tid, c, cmd, cmd_chroot, no_error=None, server_id=server.id)

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
                'server_args     = -i -l -N %(HOSTNAME)s\n'
                'port            = 30865\n'
                'type            = UNLISTED\n'
                'disable         = no\n'
                '}\n')

            inet_conf_file = os.path.join(chroot, 'etc', 'xinetd.d', 'csync2')
            inetd_conf = inetd_conf % ({'HOSTNAME': 'csync{}.gluu'.format(server.id)})
            c.put_file(inet_conf_file, inetd_conf)


        #cmd = '{} -xv -N {}'.format(csync2_path, server.hostname)
        #run_command(tid, c, cmd, chroot, no_error=None)



        #run time sync in every minute
        cron_file = os.path.join(chroot, 'etc', 'cron.d', 'csync2')
        c.put_file(cron_file,
            '{}-59/2 * * * *    root    {} -N csync{}.gluu -xv 2>/var/log/csync2.log\n'.format(
            server_counter, csync2_path, server.id))

        server_counter += 1
        
        wlogger.log(tid, 'Crontab entry was created to sync files in every minute',
                         'debug', server_id=server.id)

        if ('CentOS' in server.os) or ('RHEL' in server.os):
            cmd = 'service crond reload'
            cmd = run_cmd.format('service xinetd start')
            run_command(tid, c, cmd, cmd_chroot, no_error=None, server_id=server.id)
            cmd = run_cmd.format('service crond restart')
            run_command(tid, c, cmd, cmd_chroot, no_error='debug', server_id=server.id)

        else:
            cmd = run_cmd.format('service cron reload')
            run_command(tid, c, cmd, cmd_chroot, no_error='debug', server_id=server.id)
            cmd = run_cmd.format('service openbsd-inetd restart')
            run_command(tid, c, cmd, cmd_chroot, no_error='debug', server_id=server.id)

        c.close()

    return True

def remove_filesystem_replication_do(server, app_config, tid):

        installer = Installer(server, app_config.gluu_version, logger_tid=tid)
        if not installer.c:
            return False
        installer.run('rm /etc/cron.d/csync2')
        
        if 'CentOS' in server.os or 'RHEL' in server.os :
            installer.run('rm /etc/xinetd.d/csync2')
            services = ['xinetd', 'crond']
            
        else:
            installer.run("sed 's/^csync/#&/' -i /etc/inetd.conf")
            services = ['openbsd-inetd', 'cron']
            
        for s in services:
            installer.run('service {} restart '.format(s))
            
        installer.run('rm /var/lib/csync2/*.*')

        return True


@celery.task(bind=True)
def remove_filesystem_replication(self):
    tid = self.request.id
    
    app_config = AppConfiguration.query.first()
    servers = Server.query.all()
    
    for server in servers:
        r = remove_filesystem_replication_do(server, app_config, tid)
        if not r:
            return r

@celery.task(bind=True)
def setup_ldap_replication(self, server_id):
    """Deploys ldap replicaton

    Args:
        server_id (integer): id of server to be deployed replication
    """

    #MB: removed until openldap replication is validated

    pass



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

    if sos == 'CentOS 7' or sos == 'RHEL 7':
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

        if sos == 'CentOS 7' or sos == 'RHEL 7':
            command = "ssh -o IdentityFile=/etc/gluu/keys/gluu-console -o Port=60022 -o LogLevel=QUIET -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=yes root@localhost '{0}'".format(cmd)
        else:
            command = 'chroot {0} /bin/bash -c "{1}"'.format(chroot,
                                                         cmd)
        cin, cout, cerr = c.run(command)
        wlogger.log(tid, cmd, 'debug')
        wlogger.log(tid, cout+cerr, 'debug')


def modify_hosts(tid, c, hosts, chroot='/', server_host=None, server_id=''):
    wlogger.log(tid, "Modifying /etc/hosts", server_id=server_id)
    
    h_file = os.path.join(chroot,'etc/hosts')
    
    r, old_hosts = c.get_file(h_file)
    
    if r:
        new_hosts = modify_etc_hosts(hosts, old_hosts)
        c.put_file(h_file, new_hosts)
        wlogger.log(tid, "{} was modified".format(h_file), 'success', server_id=server_id)
    else:
        wlogger.log(tid, "Can't receive {}".format(h_file), 'fail', server_id=server_id)


    if chroot:

        h_file = os.path.join(chroot, 'etc/hosts')
        
        r, old_hosts = c.get_file(h_file)
        
        #for host in hosts:
        #    if host[0] == server_host:
        #        hosts.remove(host)
        #        break
        
        if r:
            new_hosts = modify_etc_hosts(hosts, old_hosts)
            c.put_file(h_file, new_hosts)
            wlogger.log(tid, "{} was modified".format(h_file), 'success', server_id=server_id)
        else:
            wlogger.log(tid, "Can't receive {}".format(h_file), 'fail', server_id=server_id)


def download_and_upload_custom_schema(tid, pc, c, ldap_type, gluu_server):
    """Downloads custom ldap schema from primary server and 
        uploads to current server represented by c
    Args:
        tid (string): id of the task running the command,
        pc (:object:`clustermgr.core.remote.RemoteClient`): client to be used
            for the SSH communication, representing primary server

        c (:object:`clustermgr.core.remote.RemoteClient`): client to be used
            for the SSH communication, representing current server
        ldap_type (string): type of ldapserver, either openldap or opendj
        gluu_server: Gluu server name
    """
    
    wlogger.log(tid, 'Downloading custom schema files' 
                    'from primary server and upload to this server')
    custom_schema_files = pc.listdir("/opt/{}/opt/gluu/schema/{}/".format(
                                                    gluu_server, ldap_type))

    if custom_schema_files[0]:
        
        schema_folder = '/opt/{}/opt/gluu/schema/{}'.format(
                        gluu_server, ldap_type)
        if not c.exists(schema_folder):
            c.run('mkdir -p {}'.format(schema_folder))
        
        for csf in custom_schema_files[1]:
            schema_filename = '/opt/{0}/opt/gluu/schema/{2}/{1}'.format(
                                                gluu_server, csf, ldap_type)
                                                
            stat, schema = pc.get_file(schema_filename)
            if stat:
                c.put_file(schema_filename, schema.read())
                wlogger.log(tid, 
                    '{0} dowloaded from from primary and uploaded'.format(
                                                            csf), 'debug')

                if ldap_type == 'opendj':

                    opendj_path = ('/opt/{}/opt/opendj/config/schema/'
                                '999-clustmgr-{}').format(gluu_server, csf)
                    c.run('cp {} {}'.format(schema_filename, opendj_path))
                    
            
def upload_custom_schema(tid, c, ldap_type, gluu_server):
    """Uploads custom ldap schema to server
    Args:
        tid (string): id of the task running the command,
        c (:object:`clustermgr.core.remote.RemoteClient`): client to be used
            for the SSH communication
        ldap_type (string): type of ldapserver, either openldap or opendj
        gluu_server: Gluu server name
    """
    
    custom_schema_dir = os.path.join(Config.DATA_DIR, 'schema')
    custom_schemas = os.listdir(custom_schema_dir)

    if custom_schemas:
        schema_folder = '/opt/{}/opt/gluu/schema/{}'.format(
                        gluu_server, ldap_type)
        if not c.exists(schema_folder):
            c.run('mkdir -p {}'.format(schema_folder))

        for sf in custom_schemas:
            
            local = os.path.join(custom_schema_dir, sf)
            remote = '/opt/{0}/opt/gluu/schema/{2}/{1}'.format(
                gluu_server, sf, ldap_type)
            r = c.upload(local, remote)
            if r[0]:
                wlogger.log(tid, 'Custom schame file {0} uploaded'.format(
                        sf), 'success')
            else:
                wlogger.log(tid,
                    "Can't upload custom schame file {0}: ".format(sf,
                                                            r[1]), 'error')
    
@celery.task(bind=True)
def removeMultiMasterDeployement(self, server_id):
    """Removes multi master replication deployment

    Args:
        server_id: id of server to be un-depoloyed
    """
    #MB: removed until openldap replication is validated
    
    pass


def do_disable_replication(tid, server, primary_server, app_config):


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
        "Disabling replication for {0}".format(
        server.hostname)
        )


    wlogger.log(tid, 
            "Making SSH connection to primary server {0}".format(
            primary_server.hostname), 'debug'
            )

    try:
        c.startup()
    except Exception as e:
        wlogger.log(
            tid, "Cannot establish SSH connection {0}".format(e), "warning")
        wlogger.log(tid, "Ending server setup process.", "error")
        return False

    wlogger.log(tid, "SSH connection successful", 'success')

    cmd = ('/opt/opendj/bin/dsreplication disable --disableAll --port 4444 '
            '--hostname {} --adminUID admin --adminPassword $\'{}\' '
            '--trustAll --no-prompt').format(
                            server.hostname,
                            app_config.replication_pw)

    cmd = cmd_run.format(cmd)
    run_command(tid, c, cmd, chroot)

    server.mmr = False
    db.session.commit()

    configure_OxIDPAuthentication(tid, exclude=server.id)

    wlogger.log(tid, "Checking replication status", 'debug')

    cmd = ('/opt/opendj/bin/dsreplication status -n -X -h {} '
            '-p 1444 -I admin -w $\'{}\'').format(
                    primary_server.hostname,
                    app_config.replication_pw)

    cmd = cmd_run.format(cmd)
    run_command(tid, c, cmd, chroot)

    return True

@celery.task(bind=True)
def opendj_disable_replication_task(self, server_id):
    server = Server.query.get(server_id)
    primary_server = Server.query.filter_by(primary_server=True).first()
    app_config = AppConfiguration.query.first()
    tid = self.request.id
    r = do_disable_replication(tid, server, primary_server, app_config)
    return r

@celery.task(bind=True)
def remove_server_from_cluster(self, server_id, remove_server=False, 
                                                disable_replication=True):

    app_config = AppConfiguration.query.first()
    primary_server = Server.query.filter_by(primary_server=True).first()
    server = Server.query.get(server_id)
    tid = self.request.id

    removed_server_hostname = server.hostname

    remove_filesystem_replication_do(server, app_config, tid)

    proxy_c = None

    if not app_config.external_load_balancer:
        proxy_c = RemoteClient(app_config.nginx_host, ip=app_config.nginx_ip)

        wlogger.log(tid, "Reconfiguring proxy server {}".format(
                                                            app_config.nginx_host))

        wlogger.log(tid,
                "Making SSH connection to load balancer {0}".format(
                app_config.nginx_host), 'debug'
                )

        try:
            proxy_c.startup()
        except Exception as e:
            wlogger.log(
                tid, "Cannot establish SSH connection {0}".format(e), "warning")
            wlogger.log(tid, "Ending server setup process.", "error")
            return False

        wlogger.log(tid, "SSH connection successful", 'success')

        # Update nginx
        nginx_config = make_nginx_proxy_conf(exception=server_id)
        remote = "/etc/nginx/nginx.conf"
        r = proxy_c.put_file(remote, nginx_config)
        
        if not r[0]:
            wlogger.log(tid, "An error occurred while uploadng nginx.conf.", "error")
            return False

        wlogger.log(tid, "nginx configuration updated", 'success')
        wlogger.log(tid, "Restarting nginx", 'debug')
        run_command(tid, proxy_c, 'service nginx restart')
    
    
    if not proxy_c:
        
        proxy_c = RemoteClient(app_config.cache_host, ip=app_config.cache_ip)
        
        wlogger.log(tid,
                "Making SSH connection to cache server {0}".format(
                app_config.cache_host), 'debug'
                )

        try:
            proxy_c.startup()
        except Exception as e:
            wlogger.log(
                tid, "Cannot establish SSH connection {0}".format(e), "warning")
            wlogger.log(tid, "Ending server setup process.", "error")
            return False

        wlogger.log(tid, "SSH connection successful", 'success')

        
    
    # Update Twemproxy
    wlogger.log(tid, "Updating Twemproxy configuration",'debug')
    twemproxy_conf = make_twem_proxy_conf(exception=server_id)
    remote = "/etc/nutcracker/nutcracker.yml"
    r = proxy_c.put_file(remote, twemproxy_conf)

    if not r[0]:
        wlogger.log(tid, "An error occurred while uploadng nutcracker.yml.", "error")
        return False

    wlogger.log(tid, "Twemproxy configuration updated", 'success')

    run_command(tid, proxy_c, 'service nutcracker restart')

    # Update stunnel
    proxy_stunnel_conf = make_proxy_stunnel_conf(exception=server_id)
    proxy_stunnel_conf = '\n'.join(proxy_stunnel_conf)
    remote = '/etc/stunnel/stunnel.conf'
    r = proxy_c.put_file(remote, proxy_stunnel_conf)

    if not r[0]:
        wlogger.log(tid, "An error occurred while uploadng stunnel.conf.", "error")
        return False

    wlogger.log(tid, "Stunnel configuration updated", 'success')


    os_type = get_os_type(proxy_c)

    if 'CentOS' or 'RHEL' in os_type:
        run_command(tid, proxy_c, 'systemctl restart stunnel')
    else:
        run_command(tid, proxy_c, 'service stunnel4 restart')

    proxy_c.close()


    if disable_replication:
        r = do_disable_replication(tid, server, primary_server, app_config)
        if not r:
            return False

    if remove_server:
        db.session.delete(server)


    chroot = '/opt/gluu-server-' + app_config.gluu_version

    for server in Server.query.all():
        if server.gluu_server:
        
            if server.os == 'CentOS 7' or server.os == 'RHEL 7':
                restart_command = '/sbin/gluu-serverd-{0} restart'.format(
                                    app_config.gluu_version)
            else:
                restart_command = '/etc/init.d/gluu-server-{0} restart'.format(
                                    app_config.gluu_version)


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


            csync2_config = get_csync2_config(exclude=removed_server_hostname)

            remote_file = os.path.join(chroot, 'etc', 'csync2.cfg')

            wlogger.log(tid, "Uploading csync2.cfg", 'debug')

            ct.put_file(remote_file,  csync2_config)



            wlogger.log(tid, "Restarting Gluu Server on {}".format(
                                server.hostname))

            run_command(tid, ct, restart_command)

            ct.close()

                

    db.session.commit()
    return True


def configure_OxIDPAuthentication(tid, exclude=None):
    
    #app_config = AppConfiguration.query.first()

    primary_server = Server.query.filter_by(primary_server=True).first()
    
    app_config = AppConfiguration.query.first()

    gluu_installed_servers = Server.query.filter_by(gluu_server=True).all()

    chroot_fs = '/opt/gluu-server-' + app_config.gluu_version

    pDict = {}

    for server in gluu_installed_servers:
        if server.mmr:
            laddr = server.ip if app_config.use_ip else server.hostname
            ox_auth = [ laddr+':1636' ]
            for prsrv in gluu_installed_servers:
                if prsrv.mmr:
                    if not prsrv == server:
                        laddr = prsrv.ip if app_config.use_ip else prsrv.hostname
                        ox_auth.append(laddr+':1636')
            pDict[server.hostname]= ','.join(ox_auth)


    for server in gluu_installed_servers:
        if server.mmr:
            ct = RemoteClient(server.hostname, ip=server.ip)
            try:
                ct.startup()
            except Exception as e:
                wlogger.log(
                    tid, "Cannot establish SSH connection {0}".format(e), "warning")
                wlogger.log(tid, "Ending server setup process.", "error")
            
            modifyOxLdapProperties(server, ct, tid, pDict, chroot_fs)

    oxIDP=['localhost:1636']

    for server in gluu_installed_servers:
        if not server.id == exclude:
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
                'oxIDPAuthentication entry is modified to include all '
                'replicating servers',
                'success')
    else:
        wlogger.log(tid, 'Modifying oxIDPAuthentication entry is failed: {}'.format(
                adminOlc.conn.result['description']), 'success')



@celery.task(bind=True)
def opendjenablereplication(self, server_id):

    primary_server = Server.query.filter_by(primary_server=True).first()
    task_id = self.request.id
    app_conf = AppConfiguration.query.first()

    gluu_installed_servers = Server.query.filter_by(gluu_server=True).all()

    if server_id == 'all':
        servers = Server.query.all()
    else:
        servers = [Server.query.get(server_id)]

    installer = Installer(
                    primary_server, 
                    app_conf.gluu_version, 
                    logger_task_id=task_id, 
                    server_os=primary_server.os
                    )

    if not installer.conn:
        return False

    # check if gluu server is installed
    if not installer.is_gluu_installed():
        wlogger.log(task_id, "Remote is not a gluu server.", "error")
        wlogger.log(task_id, "Ending server setup process.", "error")
        return False

    tmp_dir = os.path.join('/tmp', uuid.uuid1().hex[:12])
    os.mkdir(tmp_dir)

    wlogger.log(task_id, "Downloading opendj certificates")

    opendj_cert_files = ('keystore', 'keystore.pin', 'truststore')

    for certificate in opendj_cert_files:
        remote = os.path.join(installer.container, 'opt/opendj/config', certificate)
        local = os.path.join(tmp_dir, certificate)
        result = installer.download_file(remote, local)
        if not result:
            return False

    primary_server_secured = False

    for server in servers:
        if not server.primary_server:
            wlogger.log(task_id, "Enabling replication on server {}".format(
                                                            server.hostname))

            for base in ['gluu', 'site']:

                cmd = ('/opt/opendj/bin/dsreplication enable --host1 {} --port1 4444 '
                        '--bindDN1 \'cn=directory manager\' --bindPassword1 $\'{}\' '
                        '--replicationPort1 8989 --host2 {} --port2 4444 --bindDN2 '
                        '\'cn=directory manager\' --bindPassword2 $\'{}\' '
                        '--replicationPort2 8989 --adminUID admin --adminPassword $\'{}\' '
                        '--baseDN \'o={}\' --trustAll -X -n').format(
                            primary_server.hostname,
                            primary_server.ldap_password.replace("'","\\'"),
                            server.hostname,
                            server.ldap_password.replace("'","\\'"),
                            app_conf.replication_pw.replace("'","\\'"),
                            base,
                            )
                
                installer.run(cmd, error_exception='no base DNs available to enable replication')

                wlogger.log(task_id, "Inıtializing replication on server {}".format(
                                                                server.hostname))

                cmd = ('/opt/opendj/bin/dsreplication initialize --baseDN \'o={}\' '
                        '--adminUID admin --adminPassword $\'{}\' '
                        '--portSource 4444  --hostDestination {} --portDestination 4444 '
                        '--trustAll -X -n').format(
                            base,
                            app_conf.replication_pw.replace("'","\\'"),
                            server.hostname,
                            )

                installer.run(cmd, error_exception='no base DNs available to enable replication')

            if not primary_server_secured:

                wlogger.log(task_id, "Securing replication on primary server {}".format(
                                                                primary_server.hostname))

                cmd = ('/opt/opendj/bin/dsconfig -h {} -p 4444 '
                        ' -D  \'cn=Directory Manager\' -w $\'{}\' --trustAll '
                        '-n set-crypto-manager-prop --set ssl-encryption:true'
                        ).format(primary_server.hostname, primary_server.ldap_password.replace("'","\\'"))

                installer.run(cmd)
                
                primary_server_secured = True
                primary_server.mmr = True

            wlogger.log(task_id, "Securing replication on server {}".format(
                                                            server.hostname))
            cmd = ('/opt/opendj/bin/dsconfig -h {} -p 4444 '
                    ' -D  \'cn=Directory Manager\' -w $\'{}\' --trustAll '
                    '-n set-crypto-manager-prop --set ssl-encryption:true'
                    ).format(server.hostname, primary_server.ldap_password.replace("'","\\'"))

            installer.run(cmd)

            server.mmr = True


    db.session.commit()

    configure_OxIDPAuthentication(task_id)

    servers = Server.query.filter(Server.primary_server.isnot(True)).all()

    for server in servers:

        if not server.primary_server:

            node_installer = Installer(
                    server, 
                    app_conf.gluu_version, 
                    logger_task_id=task_id, 
                    server_os=primary_server.os
                    )

            wlogger.log(task_id, "Uploading OpenDj certificate files")
            for certificate in opendj_cert_files:
                remote = os.path.join(node_installer.container, 'opt/opendj/config', certificate)
                local = os.path.join(tmp_dir, certificate)
                result = node_installer.upload_file(local, remote)
                
                if not result:
                    return False

        node_installer.restart_gluu()

    installer.restart_gluu()

    if 'CentOS' in primary_server.os:
        wlogger.log(tid, "Waiting for Gluu to finish starting")
        time.sleep(60)
    

    wlogger.log(task_id, "Checking replication status")

    cmd = ('/opt/opendj/bin/dsreplication status -n -X -h {} '
            '-p 1444 -I admin -w $\'{}\'').format(
                    primary_server.hostname,
                    app_conf.replication_pw.replace("'","\\'"))

    installer.run(cmd)

    return True


@celery.task(bind=True)
def installNGINX(self, nginx_host):
    """Installs nginx load balancer

    Args:
        nginx_host: hostname of server on which we will install nginx
    """
    task_id = self.request.id
    app_conf = AppConfiguration.query.first()
    primary_server = Server.query.filter_by(primary_server=True).first()

    #mock server
    nginx_server = Server(
                        hostname=app_conf.nginx_host, 
                        ip=app_conf.nginx_ip,
                        os=app_conf.nginx_os
                        )

    nginx_installer = Installer(
                    nginx_server, 
                    app_conf.gluu_version, 
                    logger_task_id=task_id, 
                    server_os=nginx_server.os
                    )

    if not nginx_installer.conn:
        return False


    #check if nginx was installed on this server
    wlogger.log(task_id, "Check if NGINX installed")

    result = nginx_installer.conn.exists("/usr/sbin/nginx")

    if result:
        wlogger.log(task_id, "nginx allready exists")
    else:
        nginx_installer.epel_release()
        nginx_installer.install('nginx', inside='False')

    #Check if ssl certificates directory exist on this server
    result = nginx_installer.conn.exists("/etc/nginx/ssl/")
    if not result:
        wlogger.log(task_id, "/etc/nginx/ssl/ does not exists. Creating ...",
                            "debug")
        result = result.conn.mkdir("/etc/nginx/ssl/")
        if result[0]:
            wlogger.log(task_id, "/etc/nginx/ssl/ was created", "success")
        else:
            wlogger.log(task_id, 
                        "Error creating /etc/nginx/ssl/ {0}".format(result[1]),
                        "error")
            wlogger.log(task_id, "Ending server setup process.", "error")
            return False
    else:
        wlogger.log(task_id, "Directory /etc/nginx/ssl/ exists.", "debug")

    # we need to download ssl certifiactes from primary server.
    wlogger.log(task_id, "Making SSH connection to primary server {} for "
                     "downloading certificates".format(primary_server.hostname))

    primary_installer = Installer(
                    primary_server,
                    app_conf.gluu_version,
                    logger_task_id=task_id,
                    server_os=primary_server.os
                    )

    # get httpd.crt and httpd.key from primary server and put to this server
    for crt_file in ('httpd.crt', 'httpd.key'):
        wlogger.log(task_id, "Downloading {0} from primary server".format(crt_file), "debug")
        remote_file = '/opt/gluu-server-{0}/etc/certs/{1}'.format(app_conf.gluu_version, crt_file)
        result = primary_installer.get_file(remote_file)
                
        if not result[0]:
            return False

        file_content = result[1]

        remote_file = os.path.join("/etc/nginx/ssl/", crt_file)

        result = nginx_installer.put_file(remote_file, file_content)
        if not result:
            return False

    primary_installer.conn.close()
    
    nginx_config = make_nginx_proxy_conf()

    #put nginx.conf to server
    remote_file = "/etc/nginx/nginx.conf"
    result = nginx_installer.put_file(remote_file, nginx_config)

    if not result:
        return False

    nginx_installer.enable_service('nginx', inside=False)
    nginx_installer.start_service('nginx', inside=False)
    
    if app_conf.modify_hosts:
        
        host_ip = []
        servers = Server.query.all()

        for ship in servers:
            host_ip.append((ship.hostname, ship.ip))

        host_ip.append((app_conf.nginx_host, app_conf.nginx_ip))
        modify_hosts(task_id, nginx_installer.conn, host_ip)

    wlogger.log(task_id, "NGINX successfully installed")

def exec_cmd(command):    
    popen = subprocess.Popen(command, stdout=subprocess.PIPE)
    return iter(popen.stdout.readline, b"")


@celery.task(bind=True)
def upgrade_clustermgr_task(self):
    tid = self.request.id
    
    cmd = '/usr/bin/sudo pip install --upgrade https://github.com/GluuFederation/cluster-mgr/archive/master.zip'

    wlogger.log(tid, cmd)

    for line in exec_cmd(cmd.split()):
        wlogger.log(tid, line, 'debug')
    
    return


@celery.task(bind=True)
def register_objectclass(self, objcls):
    
    tid = self.request.id
    primary = Server.query.filter_by(primary_server=True).first()

    servers = Server.query.all()
    appconf = AppConfiguration.query.first()

    
    wlogger.log(tid, "Making LDAP connection to primary server {}".format(primary.hostname))
    
    ldp = getLdapConn(  primary.hostname,
                        "cn=directory manager",
                        primary.ldap_password
                        )
    
    r = ldp.registerObjectClass(objcls)
 
    if not r:
        wlogger.log(tid, "Attribute cannot be registered".format(primary.hostname), 'error')
        return False
    else:
        wlogger.log(tid, "Object class is registered",'success')


    for server in servers:
        installer = Installer(server, appconf.gluu_version, logger_tid=tid)
        if installer.c:
            wlogger.log(tid, "Restarting idendity at {}".format(server.hostname))
            installer.run('/etc/init.d/identity restart')
    
    appconf.object_class_base = objcls
    db.session.commit()
    
    return True

