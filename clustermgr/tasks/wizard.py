import json
import os
import getpass
import re

from clustermgr.models import Server, AppConfiguration
from clustermgr.extensions import db, wlogger, celery
from clustermgr.core.remote import RemoteClient
from clustermgr.core.utils import run_and_log
from clustermgr.core.clustermgr_installer import Installer
from clustermgr.config import Config
from clustermgr.core.utils import get_setup_properties, \
        write_setup_properties_file
from clustermgr.core.change_gluu_host import ChangeGluuHostname

from flask import current_app as app



@celery.task(bind=True)
def wizard_step1(self):
    
    """Celery task that collects information about server.

    :param self: the celery task

    :return: the number of servers where both stunnel and redis were installed
        successfully
    """
    
    task_id = self.request.id

    wlogger.log(task_id, "Analayzing Current Server")

    server = Server.query.filter_by(primary_server=True).first()

    app_conf = AppConfiguration.query.first()

    installer = Installer(
                server, 
                '', 
                logger_task_id=task_id, 
                server_os=server.os
                )

    
    os_type = installer.server_os
    server.os = os_type
    wlogger.log(task_id, "OS type was determined as {}".format(os_type), 'success')
    
    gluu_version = installer.get_gluu_version()

    if not gluu_version:
        wlogger.log(task_id, "Gluu Server is not installed on this server", 'fail')
        wlogger.log(tid, "Ending analyzation of server.", 'error')
        return False
        
    app_conf.gluu_version = gluu_version

 
    wlogger.log(task_id, "Gluu version was determined as {}".format(gluu_version), 'success')
    
    installer.gluu_version = gluu_version
    installer.settings()

    setup_properties_last = os.path.join(installer.container, 
                        'install/community-edition-setup/setup.properties.last')
    
    setup_properties_local = os.path.join(Config.DATA_DIR, 'setup.properties')
    
    result = installer.download_file(setup_properties_last, setup_properties_local)
    
    if not result:
        wlogger.log(task_id, "setup.properties.last could not be dowloade. Ending analization of server.", 'error')
        return False

    prop = get_setup_properties()
    prop['hostname'] = app_conf.nginx_host
    write_setup_properties_file(prop)

    server.ldap_password = prop['ldapPass']
    
    wlogger.log(task_id, "LDAP Bind password was identifed", 'success')
    
    db.session.commit()


@celery.task(bind=True)
def wizard_step2(self):
    tid = self.request.id

    setup_prop = get_setup_properties()
    
    server = Server.query.filter_by(primary_server=True).first()
    app_conf = AppConfiguration.query.first()
    
    c = RemoteClient(server.hostname, ip=server.ip)

    wlogger.log(tid, "Making SSH Connection")

    try:
        c.startup()
        wlogger.log(tid, "SSH connection established", 'success')
    except:
        wlogger.log(tid, "Can't establish SSH connection",'fail')
        wlogger.log(tid, "Ending changing name.", 'error')
        return
    
    name_changer = ChangeGluuHostname(
            old_host = server.hostname,
            new_host = app_conf.nginx_host,
            cert_city = setup_prop['city'],
            cert_mail = setup_prop['admin_email'], 
            cert_state = setup_prop['state'],
            cert_country = setup_prop['countryCode'],
            server = server.hostname,
            ip_address = server.ip,
            ldap_password = setup_prop['ldapPass'],
            os_type = server.os,
            gluu_version = app_conf.gluu_version
        )

    name_changer.logger_tid = tid

    r = name_changer.startup()
    if not r:
        wlogger.log(tid, "Name changer can't be started",'fail')
        wlogger.log(tid, "Ending changing name.", 'error')
        return

    wlogger.log(tid, "Cahnging LDAP Applience configurations")
    name_changer.change_appliance_config()
    wlogger.log(tid, "LDAP Applience configurations were changed", 'success')
    
    
    wlogger.log(tid, "Cahnging LDAP Clients entries")
    name_changer.change_clients()
    wlogger.log(tid, "LDAP Applience Clients entries were changed", 'success')

    wlogger.log(tid, "Cahnging LDAP Uma entries")
    name_changer.change_uma()
    wlogger.log(tid, "LDAP Applience Uma entries were changed", 'success')
    
    wlogger.log(tid, "Reconfiguring http")
    name_changer.change_httpd_conf()
    wlogger.log(tid, " LDAP Applience Uma entries were changed", 'success')
    

    wlogger.log(tid, "Creating certificates")
    name_changer.create_new_certs()
    
    wlogger.log(tid, "Changing /etc/hostname")
    name_changer.change_host_name()
    wlogger.log(tid, "/etc/hostname was changed", 'success')
    
    wlogger.log(tid, "Modifying /etc/hosts")
    name_changer.modify_etc_hosts()
    wlogger.log(tid, "/etc/hosts was modified", 'success')
    
    name_changer.installer.restart_gluu()
    
