import ldap
import time

from fabric.api import run, execute, cd, put
from fabric.contrib.files import exists
from fabric.context_managers import settings

from .application import celery, db, wlogger
from .models import LDAPServer, AppConfiguration

ldap.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)


@celery.task(bind=True)
def initialize_provider(self, server_id):
    initialized = False
    server = LDAPServer.query.get(server_id)
    appconfig = AppConfiguration.query.get(1)
    dn = appconfig.replication_dn
    replication_user = [
            ('objectclass', [r'person']),
            ('cn', [r'{}'.format(
                dn.replace("cn=", "").replace(",o=gluu", ""))]),
            ('sn', [r'gluu']),
            ('userpassword', [str(appconfig.replication_pw)])
            ]

    # Step 1: Connection
    wlogger.log(self.request.id, 'Connecting to {}'.format(server.hostname))
    try:
        con = ldap.initialize('ldap://{}:{}'.format(
            server.hostname, server.port))
        if server.starttls:
            con.start_tls_s()
        con.bind_s('cn=directory manager,o=gluu', server.admin_pw)
        wlogger.log(self.request.id, 'Connection established.', 'success',
                    step='conn')
    except ldap.LDAPError as e:
        if type(e.message) == dict and 'desc' in e.message:
            wlogger.log(self.request.id, e.message['desc'], 'error',
                        step='conn')
        else:
            wlogger.log(self.request.id, "%s" % e, 'error', step='conn')

    # Step 2: Add replication user
    wlogger.log(self.request.id, 'Adding the replication user.')
    try:
        con.add_s(dn, replication_user)
        wlogger.log(self.request.id, 'Replication user added.', 'success',
                    step='add')
    except ldap.ALREADY_EXISTS:
        con.delete_s(dn)
        con.add_s(dn, replication_user)
        wlogger.log(self.request.id, 'Replication user added.', 'success',
                    step='add')
    except ldap.LDAPError as e:
        if type(e.message) == dict and 'desc' in e.message:
            wlogger.log(self.request.id, e.message['desc'], 'error',
                        step='add')
        else:
            wlogger.log(self.request.id, "%s" % e, 'error', step='add')
    finally:
        con.unbind()

    # Step 3: Reconnect as replication user
    wlogger.log(self.request.id, 'Authenticating as the Replicaiton DN.')
    try:
        con = ldap.initialize('ldap://{}:{}'.format(
            server.hostname, server.port))
        if server.starttls:
            con.start_tls_s()
        con.bind_s(dn, appconfig.replication_pw)
        wlogger.log(self.request.id, 'Reconnecting as the replication user.',
                    'success', step='recon')
        initialized = True
    except ldap.LDAPError as e:
        if type(e.message) == dict and 'desc' in e.message:
            wlogger.log(self.request.id, e.message['desc'], 'error',
                        step='recon')
        else:
            wlogger.log(self.request.id, "%s" % e, 'error', step='recon')
    finally:
        con.unbind()

    if initialized:
        server.initialized = True
        db.session.add(server)
        db.session.commit()


@celery.task(bind=True)
def replicate(self):
    taskid = self.request.id
    dn = 'cn=testentry,o=gluu'
    replication_user = [
            ('objectclass', ['person']),
            ('cn', ['testentry']),
            ('sn', ['gluu']),
            ]

    wlogger.log(taskid, 'Listing all providers')
    providers = LDAPServer.query.filter_by(role="provider").all()
    wlogger.log(taskid, 'Available providers: {}'.format(len(providers)))

    for provider in providers:
        # connect to the server
        procon = ldap.initialize('ldap://{}:{}'.format(
            provider.hostname, provider.port))
        try:
            if provider.starttls:
                procon.start_tls_s()
            procon.bind_s('cn=directory manager,o=gluu', provider.admin_pw)
            wlogger.log(taskid, 'Connecting to the provider: {}'.format(
                provider.hostname), 'success')
            # add a entry to the server
            procon.add_s(dn, replication_user)
            wlogger.log(taskid,
                        'Adding the test entry {} to the provider'.format(dn),
                        'success')
        except ldap.LDAPError as e:
            wlogger.log(taskid,
                        'Failed to add test data to provider. {}'.format(e),
                        'error')
            continue

        consumers = provider.consumers
        wlogger.log(taskid,
                    'Listing consumers linked to the provider {}'.format(
                        provider.hostname))
        # get list of all the consumers
        for consumer in consumers:
            wlogger.log(taskid, 'Verifying data in consumers: {} of {}'.format(
                    consumers.index(consumer)+1, len(consumers)))
            con = ldap.initialize('ldap://{}:{}'.format(consumer.hostname,
                                                        consumer.port))
            try:
                if consumer.starttls:
                    con.start_tls_s()
                con.bind_s('cn=directory manager,o=gluu', consumer.admin_pw)
                wlogger.log(taskid, 'Connecting to the consumer: {}'.format(
                    consumer.hostname), 'success')
            except ldap.LDAPError as e:
                wlogger.log(taskid, 'Failed to connect to {0}. {1}'.format(
                                    consumer.hostname, e), 'error')
                continue

            # fetch the data from each consumer and verify the new entry exists
            for i in range(5):
                if con.compare_s(dn, 'sn', 'gluu'):
                    wlogger.log(taskid,
                                'Test data is replicated and available.',
                                'success')
                    break
                else:
                    wlogger.log(taskid,
                                'Test data not found. Retrying in 3 secs.',
                                'error')
                    time.sleep(3)
            con.unbind()

        # delete the entry from the provider
        persists = False
        try:
            procon.delete_s(dn)
            persists = procon.compare_s(dn, 'sn', 'gluu')
            if persists:
                wlogger.log(taskid, 'Delete operation failed. Data exists.',
                            'error')
        except ldap.NO_SUCH_OBJECT:
            wlogger.log(taskid, 'Deleting test data from provider: {}'.format(
                provider.hostname), 'success')
        except ldap.LDAPError as e:
            wlogger.log(taskid,
                        'Failed to delete test data from provider: {}'.format(
                            provider.hostname), 'error')
        finally:
            procon.unbind()

        # verify the data is removed from the consumers
        for consumer in consumers:
            wlogger.log(
                taskid,
                "Verifying data is removed from consumers: {} of {}".format(
                    consumers.index(consumer)+1, len(consumers)))
            con = ldap.initialize('ldap://{}:{}'.format(consumer.hostname,
                                                        consumer.port))
            persists = False
            try:
                if consumer.starttls:
                    con.start_tls_s()
                persists = con.compare_s(dn, 'sn', 'gluu')
                if persists:
                    wlogger.log(
                        taskid,
                        'Failed to remove test data from consumer: {}'.format(
                            consumer.hostname), 'error')
                else:
                    wlogger.log(
                        taskid,
                        'Test data removed from the consumer: {}'.format(
                            consumer.hostname), 'success')
            except ldap.LDAPError as e:
                wlogger.log(
                    taskid, 'Failed to test consumer: {0}. Error: {1}'.format(
                        consumer.hostname, e), 'error')
            finally:
                con.unbind()

    wlogger.log(taskid, 'Replication test Complete.', 'success')


def generate_slapd(taskid, conffile):
    wlogger.log(taskid, "Copying slapd.conf file to remote server")
    log = "> "+str(put(conffile, '/opt/symas/etc/openldap/slapd.conf'))
    wlogger.log(taskid, log)
    status = run('service solserver status')
    wlogger.log(taskid, "Checking Status of solserver\n> "+status)
    if 'is running' in status:
        log = run('service solserver stop')
        wlogger.log(taskid, "{0}\n> {1}".format(log.command, log))
    with cd('/opt/symas/etc/openldap/'):
        wlogger.log(taskid, "Generating slad.d Online Configuration")
        run('rm -rf slapd.d')
        run('mkdir slapd.d')
        log = run('/opt/symas/bin/slaptest -f slapd.conf -F slapd.d')
        wlogger.log(taskid, "{0}\n> {1}".format(log.command, log))
    wlogger.log(taskid, "Starting solserver")
    log = run('service solserver start')
    wlogger.log(taskid, "{0}\n> {1}".format(log.real_command, log))
    if 'failed' in log:
        wlogger.log(taskid, "Debugging slapd...")
        log = run("/opt/symas/lib64/slapd -d 1 "
                  "-f /opt/symas/etc/openldap/slapd.conf")
        wlogger.log(taskid, "{0}\n> {1}".format(log.real_command, log))


@celery.task(bind=True)
def setup_server(self, server_id, conffile):
    server = LDAPServer.query.get(server_id)
    host = "root@{}".format(server.hostname)
    with settings(warn_only=True):
        execute(generate_slapd, self.request.id, conffile, hosts=[host])


def check_certificates(taskid, server):
    wlogger.log(taskid, "Checking for Server Certificates.")
    if exists(server.tls_cacert):
        wlogger.log(taskid, "TLS CA Cert: {}".format(server.tls_cacert),
                    "success")
    else:
        wlogger.log(taskid, "TLS CA Cert: {}".format(server.tls_cacert),
                    "error")
    if exists(server.tls_servercert):
        wlogger.log(taskid, "TLS Server Cert:{}".format(server.tls_servercert),
                    "success")
    else:
        wlogger.log(taskid, "TLS Server Cert:{}".format(server.tls_servercert),
                    "error")
    if exists(server.tls_serverkey):
        wlogger.log(taskid, "TLS Server Key: {}".format(server.tls_serverkey),
                    "success")
    else:
        wlogger.log(taskid, "TLS Server Key: {}".format(server.tls_serverkey),
                    "error")


def check_ldap_data_directories(taskid, server):
    wlogger.log(taskid, "Checking for LDAP Data Directories")
    if exists('/opt/gluu/data/main_db'):
        wlogger.log(taskid, "Main data dir: /opt/gluu/data/main_db", "success")
    else:
        wlogger.log(taskid, "Missing main data dir /opt/gluu/data/main_db",
                    "error")
        wlogger.log(taskid, "Creating main data dir /opt/gluu/data/main_db")
        run('mkdir -p /opt/gluu/data/main_db')

    if exists('/opt/gluu/data/accesslog'):
        wlogger.log(taskid, "Accesslog dir: /opt/gluu/data/accesslog",
                    "success")
    else:
        wlogger.log(taskid, "Missing Accesslog dir /opt/gluu/data/accesslog",
                    "error")
        wlogger.log(taskid, "Creating Accesslog dir /opt/gluu/data/accesslog")
        run('mkdir -p /opt/gluu/data/accesslog')


@celery.task(bind=True)
def check_requirements(self, server_id):
    # check for the following TODO
    # 1. existance of all the certificate files
    # 2. existance of the default data directories
    server = LDAPServer.query.get(server_id)
    host = "root@{}".format(server.hostname)
    with settings(warn_only=True):
        # Check certificates
        execute(check_certificates, self.request.id, server, hosts=[host])
        # Check LDAP data directories