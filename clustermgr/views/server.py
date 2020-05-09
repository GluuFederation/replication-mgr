# -*- coding: utf-8 -*-

import os
import time
import glob
import ldap3

from flask_wtf import FlaskForm
from wtforms import BooleanField

from flask import Blueprint, render_template, redirect, url_for, \
    flash, request, jsonify, current_app

from flask_login import login_required

from clustermgr.extensions import db
from clustermgr.models import ConfigParam


from clustermgr.forms import ServerForm, InstallServerForm, \
    SetupPropertiesLastForm, GluuVersionForm

from clustermgr.tasks.server import collect_server_details, \
    task_install_gluu_server, task_add_service, task_test
from clustermgr.config import Config

from clustermgr.tasks.cluster import remove_server_from_cluster


from clustermgr.core.remote import RemoteClient, ClientNotSetupException
from ..core.license import license_required
from ..core.license import license_reminder
from ..core.license import prompt_license

from clustermgr.core.utils import parse_setup_properties, \
    write_setup_properties_file, get_setup_properties, \
    port_status_cmd, as_boolean

from clustermgr.core.ldap_functions import getLdapConn


server_view = Blueprint('server', __name__)
server_view.before_request(prompt_license)
server_view.before_request(license_required)
server_view.before_request(license_reminder)


def sync_ldap_passwords(password):
    non_primary_servers = Server.query.filter(
        Server.primary_server.isnot(True)).all()
    for server in non_primary_servers:
        server.ldap_password = password
    db.session.commit()


@server_view.route('/', methods=['GET', 'POST'])
@login_required
def index():
    """Route for URL /server/. GET returns ServerForm to add a server,
    POST accepts the ServerForm, validates and creates a new Server object
    """
    
    load_balancer_config = ConfigParam.get('load_balancer')
    if not load_balancer_config:
        return redirect(url_for('load_balancer.config', next=url_for('server.index')))

    settings = ConfigParam.get('settings')
    if not settings:
        return redirect(url_for('server.settings', next=url_for('server.index')))


    primary_server = ConfigParam.get_primary_server()

    form = ServerForm()
    header = "New Server"
    
    if primary_server:
        del form.ldap_password
        del form.ldap_password_confirm
    else:
        header = "New Server - Primary Server"

    if form.validate_on_submit():

        server = ConfigParam.new(
                    'gluuserver', 
                    data={
                        'hostname': form.hostname.data.strip(),
                        'ip': form.ip.data.strip(),
                        'primary': not primary_server,
                        'mmr': False,
                        }
                    )

        ask_passphrase = False

        c = RemoteClient(server.data.hostname, server.data.ip)

        try:
            c.startup()

        except ClientNotSetupException as e:

            if str(e) == 'Pubkey is encrypted.':
                ask_passphrase = True
                flash("Pubkey seems to password protected. "
                    "After setting your passphrase re-submit this form.",
                    'warning')
            elif str(e) == 'Could not deserialize key data.':
                ask_passphrase = True
                flash("Password you provided for pubkey did not work. "
                    "After setting your passphrase re-submit this form.",
                    'warning')
            else:
                flash("SSH connection to {} failed. Please check if your pub key is "
                    "added to /root/.ssh/authorized_keys on this server. Reason: {}".format(
                                                    server.hostname, e), 'error')

        
        #except:
        #    flash("SSH connection to {} failed. Please check if your pub key is "
        #        "added to /root/.ssh/authorized_keys on this server".format(
        #                                            server.hostname))
        
            print("ask_passphrase", ask_passphrase)
        
            return render_template('new_server.html',
                       form=form,
                       header=header,
                       server_id=None,
                       ask_passphrase=ask_passphrase,
                       next=url_for('server.index')
                       )

        if primary_server:
            server.data.ldap_password = primary_server.data.ldap_password
        else:
            server.data.ldap_password = form.ldap_password.data.strip()
        
        server.save()
        
        collect_server_details.delay(server.id)
        
        return redirect(url_for('index.home'))

    return render_template('new_server.html',
                           form=form,
                           header=header,
                           server_id=None)


@server_view.route('/edit/<int:server_id>/', methods=['GET', 'POST'])
@login_required
def edit(server_id):
    server = ConfigParam.get_by_id(server_id)
    if not server:
        flash('There is no server with the ID: %s' % server_id, "warning")
        return redirect(url_for('index.home'))

    form = ServerForm()
    header = "Update Server Details"
    if server.data.primary:
        header = "Update Primary Server Details"
        if request.method == 'POST' and not form.ldap_password.data.strip():
            form.ldap_password.data = '**dummy**'
            form.ldap_password_confirm.data = '**dummy**'
    else:
        del form.ldap_password
        del form.ldap_password_confirm


    if request.method == 'POST' and (form.hostname.data.strip() == server.data.hostname):
        form.samehost = True


    if form.validate_on_submit():
        server.data.hostname = form.hostname.data.strip()
        server.data.ip = form.ip.data.strip()

        
        if server.data.primary and form.ldap_password.data != '**dummy**':
            server.data.ldap_password = form.ldap_password.data.strip()
            sync_ldap_passwords(server.data.ldap_password)
        server.save()
        # start the background job to get system details
        collect_server_details.delay(server.id)
        return redirect(url_for('index.home'))

    form.hostname.data = server.data.hostname
    form.ip.data = server.data.ip
    if server.data.primary:
        form.ldap_password.data = server.data.ldap_password

    return render_template('new_server.html', form=form, header=header)


@server_view.route('/remove/<int:server_id>')
@login_required
def remove_server(server_id):

    servers = ConfigParam.get_servers()

    if len(servers) > 1:
        if servers[0].id == server_id:
            flash("Please first remove non-primary servers ", "danger")
            return redirect(url_for('index.home'))

    server = ConfigParam.get_by_id(server_id)

    if request.args.get('removefromdashboard') == 'true':
        server.delete()
        flash("Server {0} is removed.".format(server.data.hostname), "success")
        return redirect(url_for('index.home'))

    disable_replication = True if request.args.get('disablereplication') == \
                               'true' else False

    return redirect(url_for('server.remove_server_from_cluster_view', 
                            server_id=server_id, 
                            disablereplication='true',
                            removeserver='true',
                            )
                    )
 
    # TODO LATER perform checks on ther flags and add their cleanup tasks

    return redirect(url_for('index.home'))



@server_view.route('/removeserverfromcluster/<int:server_id>/')
def remove_server_from_cluster_view(server_id):
    """Initiates removal of replications"""
    remove_server = False

    if request.args.get('removeserver'):
        remove_server = True
    
    disable_replication = True if request.args.get(
                                    'disablereplication',''
                                    ).lower() == 'true' else False

    #Start non-gluu ldap server installation celery task
    server = ConfigParam.get_by_id(server_id)
    task = remove_server_from_cluster.delay(
                                            server.id, 
                                            remove_server, 
                                            disable_replication
                                        )

    title = "Removing server {} from cluster".format(server.data.hostname)

    if request.args.get('next') == 'dashboard':
        nextpage = url_for("index.home")
        whatNext = "Dashboard"
    else:
        nextpage = url_for("index.home")
        whatNext = "Multi Master Replication"

    return render_template('logger_single.html',
                           server_id=server_id,
                           title=title,
                           steps=[],
                           task=task,
                           cur_step=1,
                           auto_next=False,
                           multistep=False,
                           nextpage=nextpage,
                           whatNext=whatNext
                           )

@server_view.route('/installgluu/<int:server_id>/', methods=['GET', 'POST'])
@login_required
def install_gluu(server_id):
    """Gluu server installation view. This function creates setup.properties
    file and redirects to install_gluu_server which does actual installation.
    """
    pserver = ConfigParam.get_primary_server()
    
    if (pserver.id != server_id) and (not pserver.data.get('gluu_server')):
        flash("Please first install primary server.", "warning")
        return redirect(url_for('index.home'))

    settings = ConfigParam.get('settings')
    if not settings:
        return redirect(url_for('server.settings',
                                next=url_for('server.install_gluu', server_id=server_id)))

    # If current server is not primary server, first we should identify
    # primary server. If primary server is not installed then redirect
    # to home to install primary.
    if not pserver:
        flash("Please identify primary server before starting to install Gluu "
              "Server.", "warning")
        return redirect(url_for('index.home'))


    server = ConfigParam.get_by_id(server_id)

    # We need os type to perform installation. If it was not identified,
    # return to home and wait until it is identifed.
    if not server.data.os:
        flash("Server OS version hasn't been identified yet. Checking Now",
              "warning")
        collect_server_details.delay(server_id)
        return redirect(url_for('index.home'))

    # If current server is not primary server, and primary server was installed,
    # start installation by redirecting to cluster.install_gluu_server
    if not server.data.primary:

        return redirect(url_for('server.install_gluu_server',
                                server_id=server_id))

    # If we come up here, it is primary server and we will ask admin which
    # components will be installed. So prepare form by InstallServerForm
    form = InstallServerForm()

    # We don't require these for server installation. These fields are required
    # for adding new server.
    del form.hostname
    del form.ip_address
    del form.ldap_password

    header = 'Install Gluu Server on {0}'.format(server.data.hostname)
    lb = ConfigParam.get('load_balancer')
    # Get default setup properties.
    setup_prop = get_setup_properties()

    setup_prop['hostname'] = lb.data.hostname
    setup_prop['ip'] = server.data.ip
    setup_prop['ldapPass'] = server.data.ldap_password

    # If form is submitted and validated, create setup.properties file.
    if form.validate_on_submit():
        setup_prop['countryCode'] = form.countryCode.data.strip()
        setup_prop['state'] = form.state.data.strip()
        setup_prop['city'] = form.city.data.strip()
        setup_prop['orgName'] = form.orgName.data.strip()
        setup_prop['admin_email'] = form.admin_email.data.strip()
        setup_prop['application_max_ram'] = str(form.application_max_ram.data)

        for o in ('installOxAuth',
                  'installOxTrust',
                  'installLdap',
                  'installHTTPD',
                  'installSaml',
                  'installOxAuthRP',
                  'installPassport',
                  'installOxd',
                  'installCasa',
                  'application_max_ram',
                  ):
            setup_prop[o] = getattr(form, o).data
        setup_prop['ldap_type'] = 'opendj'
        setup_prop['opendj_type'] = 'wrends'
        setup_prop['installJce'] = True
        setup_prop['installLdap'] = True

        if setup_prop['installCasa']:
            setup_prop['installOxd'] = True
            
        write_setup_properties_file(setup_prop)

        # Redirect to cluster.install_gluu_server to start installation.
        
        return redirect(url_for('server.confirm_setup_properties', server_id=server_id))
        
        #return redirect(url_for('cluster.install_gluu_server',
        #                        server_id=server_id))

    # If this view is requested, rather than post, display form to
    # admin to determaine which elements to be installed.
    if request.method == 'GET':
        form.countryCode.data = setup_prop['countryCode']
        form.state.data = setup_prop['state']
        form.city.data = setup_prop['city']
        form.orgName.data = setup_prop['orgName']
        form.admin_email.data = setup_prop['admin_email']
        form.application_max_ram.data = setup_prop['application_max_ram']

        for o in ('installOxAuth',
                  'installOxTrust',
                  'installLdap',
                  'installHTTPD',
                  'installSaml',
                  'installOxAuthRP',
                  'installPassport',
                  'installCasa',
                  'installOxd',
                  
                  ):
            getattr(form, o).data = as_boolean(setup_prop.get(o, ''))

    setup_properties_form = SetupPropertiesLastForm()

    return render_template('new_server.html',
                           form=form,
                           server_id=server_id,
                           setup_properties_form=setup_properties_form,
                           header=header)


@server_view.route('/uploadsetupproperties/<int:server_id>', methods=['POST'])
def upload_setup_properties(server_id):
    setup_properties_form = SetupPropertiesLastForm()

    if setup_properties_form.upload.data and \
            setup_properties_form.validate_on_submit():

        f = setup_properties_form.setup_properties.data
        try:
            setup_prop = parse_setup_properties(f.stream)
        except:
            flash("Can't parse, please upload valid setup.properties file.",
                    "danger")
            return redirect(url_for('install_gluu', server_id=1))
            
        for prop in (
                    'countryCode', 'orgName', 'application_max_ram', 'city',
                    'state', 'admin_email'):

            if not prop in setup_prop:
                flash("'{0}' is missing, please upload valid setup.properties file.".format(prop),
                "danger")
                return redirect(url_for('server.install_gluu', server_id=1))

            if rf in get_proplist():
                del setup_prop[rf]

        appconf = AppConfiguration.query.first()
        server = Server.query.get(server_id)

        setup_prop['hostname'] = appconf.nginx_host
        setup_prop['ip'] = server.ip
        setup_prop['ldapPass'] = server.ldap_password
        setup_prop['ldap_type'] = 'opendj'
        
        write_setup_properties_file(setup_prop)

        flash("Setup properties file has been uploaded sucessfully. ",
              "success")

        return redirect(url_for('server.confirm_setup_properties', server_id=server_id))
        
        #return redirect(url_for('cluster.install_gluu_server',
        #                       server_id=server_id))
    else:
        flash("Please upload valid setup properties file", "danger")

        
        
        return redirect(url_for('server.install_gluu', server_id=server_id))


@server_view.route('/confirmproperties/<int:server_id>/', methods=['GET'])
@login_required
def confirm_setup_properties(server_id):

    setup_prop = get_setup_properties()

    load_balancer = ConfigParam.get('load_balancer')
    server = ConfigParam.get_by_id(server_id)

    setup_prop['hostname'] = load_balancer.data.hostname
    setup_prop['ip'] = server.data.ip
    setup_prop['ldapPass'] = server.data.ldap_password

    keys = list(setup_prop.keys())
    keys.sort()

    return render_template(
                        'server_confirm_properties.html',
                        server_id = server_id,
                        setup_prop = setup_prop,
                        keys = keys,
                    )


@server_view.route('/addservice/', methods=['GET', 'POST'])
@login_required
def add_service():

    services = (
            'installOxAuth',
            'installOxTrust',
            'installSaml',
            'installPassport',
            #'installGluuRadius',
            'installCasa',
            'installOxd',
        )

    prop = get_setup_properties()

    if request.method == 'GET':

        class authForm(FlaskForm):
            pass


        for s in services:
            default = True if as_boolean(prop.get(s, 'false')) else False
            f = BooleanField(s.replace('install',''), default=default)
            setattr(authForm, s, f)

        form = authForm()

        for s in services:
            f = getattr(form, s)
            f.disabled = True if as_boolean(prop.get(s, 'false')) else False
        
        return render_template('simple_form.html', form=form)

    else:
        
        services_to_install = []
        for s in services:
            if request.form.get(s):
                services_to_install.append(s)
        
        if not services_to_install:
            flash("Nothing was selected to install", "success")
            return redirect(url_for('index.home'))
        
        servers = ConfigParam.get_servers()
        
        task = task_add_service.delay(services_to_install)

        title = 'Installing Services'
        nextpage=url_for('index.home')
        whatNext="Dashboard"
        
        return render_template('logger_single.html',
                           task_id=task.id, title=title,
                           nextpage=nextpage, whatNext=whatNext,
                           task=task, multiserver=servers,
                           )

@server_view.route('/ldapstat/<int:server_id>/')
def get_ldap_stat(server_id):

    server = ConfigParam.get_by_id(server_id)
    if server:
        try:
            ldap_server = ldap3.Server('ldaps://{}:1636'.format(server.data.hostname))
            conn = ldap3.Connection(ldap_server)
            if conn.bind(): 
                return "1"
        except:
            pass
    return "0"


def test_port(server, client, port):
    try:
        channel = server.client.get_transport().open_session()
        channel.get_pty()
        cmd = '''python -c "import time, socket;sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM); sock.bind(('{}', {})); sock.listen(5); time.sleep(20)"'''.format(server.ip, port)

        channel.exec_command(cmd)
        i = 1
        while True:
            if channel.exit_status_ready():
                break
            time.sleep(0.1)
            cmd2 = port_status_cmd.format(server.ip, port)
            r = client.run(cmd2)
            if r[1].strip()=='0':
                return True
            i += 1
            if i > 5:
                break
    except:
        return False


@server_view.route('/dryrun/<int:server_id>')
def dry_run(server_id):
    
    #return jsonify({'nginx': {'port_status': {7777: True, 1636: True, 80: True, 30865: True, 1689: True, 443: True, 4444: False, 8989: True}, 'ssh': True}, 'server': {'port_status': {7777: True, 1636: True, 80: True, 30865: True, 1689: True, 443: True, 4444: False, 8989: False}, 'ssh': True}})
    
    
    result = {'server':{'ssh':False, 'port_status':{}}, 'nginx':{'ssh':False, 'port_status':{}}}
    server_ports = [16379, 443, 4444, 1636, 80, 8989, 7777, 30865]
    
    for p in server_ports:
        result['nginx']['port_status'][p] = False
        result['server']['port_status'][p] = False
    
    server = Server.query.get(server_id)
    appconf = AppConfiguration.query.first()

    c = RemoteClient(server.hostname, server.ip)

    try:
        c.startup()
        result['server']['ssh']=True
    except:
        pass

    if result['server']['ssh']:
        #Test is any process listening ports that will be used by gluu-server
        for p in server_ports:
            cmd = port_status_cmd.format(server.ip, p)
            r = c.run(cmd)
            if r[1].strip()=='0':
                result['server']['port_status'][p] = True

        if appconf.external_load_balancer:
            c_host = appconf.cache_host
            c_ip = appconf.cache_ip
        else:
            c_host = appconf.nginx_host
            c_ip = appconf.nginx_ip

        c_nginx = RemoteClient(c_host, c_ip)
        try:
            c_nginx.startup()
            result['nginx']['ssh']=True
        except:
            pass

        if result['nginx']['ssh']:
            for p in server_ports:
                if not result['server']['port_status'][p]:
                    r = test_port(c, c_nginx, p)
                    if r:
                        result['nginx']['port_status'][p] = True
                else:
                    cmd = port_status_cmd.format(server.ip, p)
                    r = c_nginx.run(cmd)
                    if r[1].strip()=='0':
                        result['nginx']['port_status'][p] = True

    return jsonify(result)


@server_view.route('/makeprimary/<int:server_id>', methods=['GET'])
@login_required
def make_primary(server_id):
    cur_primary = Server.query.filter_by(primary_server=True).first()
    
    if cur_primary:
        cur_primary.primary_server = None
    server = Server.query.get(server_id)
    
    if server:
        server.primary_server = True
    
    db.session.commit()
    
    flash("Server {} was set as Primary Server".format(server.hostname), "info")
    
    return redirect(url_for('index.home'))


@server_view.route('/installgluuserver/<int:server_id>/', methods=['GET'])
@login_required
def install_gluu_server(server_id):
    
    print("Installing Gluu Server")
    
    task = task_install_gluu_server.delay(server_id)
    
    steps = ['Perpare Server', 'Install Gluu Container', 'Run setup.py', 'Post Installation']

    server = ConfigParam.get_by_id(server_id)

    title = "Installing Gluu Server on " + server.data.hostname

    nextpage = url_for('index.home')
    whatNext = "Dashboard"

    return render_template('logger_single.html',
                           server_id=server_id,
                           title=title,
                           steps=steps,
                           task=task,
                           cur_step=1,
                           auto_next=True,
                           multistep=True,
                           nextpage=nextpage,
                           whatNext=whatNext
                           )

@server_view.route('/settings', methods=['GET','POST'])
@login_required
def settings():

    outside = request.args.get('outside')
    cform = GluuVersionForm()
    next_url = request.args.get('next')
    cform.gluu_archive.choices = [ (f,os.path.split(f)[1]) for f in glob.glob(os.path.join(Config.GLUU_REPO,'gluu-server*')) ]
    
    primary_server = ConfigParam.get_primary_server()
    is_primary_deployed = False
    if primary_server:
        is_primary_deployed = primary_server.data.get('gluu_server', False)

    settings = ConfigParam.get('settings')
    if not settings:
        settings = ConfigParam.new('settings',
                    data = {
                        'gluu_version': cform.gluu_version.default,
                        'gluu_archive': '',
                        'service_update_period': 300,
                        'modify_hosts': True,
                        'offline': False,
                        'use_ldap_cache': True,
                        }
                    )

    if request.method == 'GET':
        cform.gluu_version.data = settings.data.gluu_version
        cform.gluu_archive.data = settings.data.gluu_archive
        cform.offline.data = settings.data.offline
        cform.service_update_period.data = str(settings.data.service_update_period)
        cform.modify_hosts.data = settings.data.modify_hosts

    else:
        if not is_primary_deployed:
            settings.data.gluu_version = cform.gluu_version.data

        settings.data.offline = cform.offline.data
        settings.data.gluu_archive = cform.gluu_archive.data if settings.data.offline else ''
        
        settings.data.service_update_period = int(cform.service_update_period.data)
        settings.data.modify_hosts = cform.modify_hosts.data

        settings.save()
        flash("Settings saved", "success")

        if next_url:
            return redirect(next_url)

        return redirect(url_for('index.home'))

    template = 'settingsc.html' if (next_url or outside) else 'settings.html'

    return render_template(template,
                            cform=cform,
                            is_primary_deployed=is_primary_deployed,
                            repo_dir = Config.GLUU_REPO,
                            next=next_url,
                            )

@server_view.route('/test', methods=['GET'])
def test_view():
    
    task = task_test.delay()
    
    steps = ['Perpare Server', 'Install Gluu Container', 'Run setup.py', 'Post Installation']

    server = Server.query.first()
    servers = Server.query.all()
    title = "You should not come this page!!!"

    nextpage = url_for('index.home')
    whatNext = "Dashboard"

    return render_template('logger_single.html',
                           title=title,
                           steps=steps,
                           task=task,
                           cur_step=2,
                           auto_next=False,
                           multiserver=servers,
                           nextpage=nextpage,
                           whatNext=whatNext
                           )

@server_view.route('/getostype', methods=['GET'])
@login_required
def get_os_type():
    servers = ConfigParam.get_servers()

    data = {}
    for server in servers:
        data[str(server.id)] = server.data.os

    return jsonify(data)
