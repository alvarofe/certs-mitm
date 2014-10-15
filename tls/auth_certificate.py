

# Copyright (C) 2014       Alvaro Felipe Melchor (alvaro.felipe91@gmail.com)

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


#Here all related with the verification through certficate

#from utils import util
import M2Crypto.X509
from M2Crypto.X509 import FORMAT_DER

import logging
import threading
import nss.nss as nss
import os
import platform
from termcolor import colored
from db.database import database
from tls.ocsp import Ocsp
import subprocess, tempfile
from nss.error import NSPRError
from  pyasn1.codec.der import decoder
from pyasn1_modules import  rfc2459
from pyasn1.type import univ
import hashlib
from ct.proto import client_pb2



from config import Config

#Configuration stuff
f = open('config/config.cfg')
cfg = Config(f)
f.close()
#


intended_usage = nss.certificateUsageSSLServer

########      LOG     #########
LOGFILENAME = 'cert.log'
LOG_DIR = cfg.config.LOG_DIR
try:
    os.mkdir(os.path.expanduser(LOG_DIR))
except OSError:
    # This error means that the directoy exists
    pass
logPath = os.path.expanduser(LOG_DIR) + LOGFILENAME
logging.basicConfig(filename=logPath,format='%(asctime)s --  %(levelname)s:%(message)s', level=logging.INFO)
########      LOG     #########


system = platform.system()
if system == "Darwin":
    from pync import Notifier

#TODO # add configuration and log and sendemail when a mitm happened

class AuthCertificate(threading.Thread):
    """
    This class validate the authentication through certificate
    """

    def __init__(self, certificates,queue, screen_lock):
        """
        Constructor of AuthCertificate

        Parameters:
            -certificates: The certificate chain
            -queue: Here we will return the result of our verification
            -screen_lock: Lock used to print in the screen
        """

        threading.Thread.__init__(self)
        self.lock = screen_lock
        self.queue = queue
        self.certs = certificates

        self.cert_nss = list()

        self.certdb_dir = os.path.expanduser(cfg.config.NSS_DB_DIR)

        nss.nss_init(self.certdb_dir)
        self.certdb = nss.get_default_certdb()

        for i in xrange(0,len(self.certs)):
            try:
                self.cert_nss.append(nss.Certificate(self.certs[i],self.certdb))
            except:
                # print 'Exception here'
                break

        try:
            self.ocsp =  Ocsp(self.certs[1],self.certs[0])
        except:
            pass

        db_name = cfg.db.db_name
        self.db_pin = database(db_name, "pinning")
        self.db_log = database(db_name, "log")
        self.db_blacklist = database(db_name, "blacklist")


    def run(self):
        if len(self.cert_nss) == 1:
            #Could be this an attack??
            cad =  'We need all the chain to validate --> ' + self.cert_nss[0].make_ca_nickname()
            with self.lock:
                print colored(cad,'red')
            return
        validation = cfg.validation

        #based on our configuration file we applie different methods to use

        if validation.rfc == True:
            self.verify_cert_through_rfc()
        if validation.dnssec == True:
            self.verify_dnssec_tlsa()
        if validation.pinning == True:
            self.verify_cert_with_pinning()
        if validation.icsi == True:
            self.verify_cert_with_icsi_notary()
        if validation.ocsp == True:
            self.verify_ocsp()
        if validation.ct == True:
            self.verify_ct()
        if validation.blacklist == True:
            self.verify_ssl_blacklist()


    def verify_ssl_blacklist(self):
        """
        We take our der-certificate and compare the fingerprint against the sslblacklist to ensure
        that we are not connected to malware site
        """
        name = self.cert_nss[0].make_ca_nickname()
        s = hashlib.new("sha1")
        s.update(self.certs[0])
        fingerprint = s.hexdigest()
        query = self.db_blacklist.get(fingerprint)
        if query == None:
            with self.lock:
                print 'The certificate %s is  safe against SSL-BLACKLIST database' % (name)
        else:
            with self.lock:
                print 'You connected a site that uses a Certificate (%s) that match with malware-certificate' % (name)
        pass


    def verify_ct(self):
	"""
	Certificate Transparency Log

        If we read in the RFC.

        <whe should validate the SCT by computing the signature input from the SCT data as well as the
        certificate and verifying the signature, using the corresponding log's public key.
        NOTE that this document does not describe how clients obtain the log' public keys.
        TLS clients MUST reject SCTs whose timestamp is in the future.>

        For now the only thing that we can do is to ensure that the timestamp ain't in the future.
        Because there isn't way to know the certificate's server log to extract the public key
	"""
        #TODO parse SignedCertificateTimestamp

        cert_sct = client_pb2.SignedCertificateTimestamp()
        #self.ocsp.check_certificate_transparency()
        cert, _ = decoder.decode(self.certs[0],asn1Spec=rfc2459.Certificate())
        tbsCertificate = cert.getComponentByName('tbsCertificate')
        extensions = tbsCertificate.getComponentByName('extensions')
        sct = None
        for ext in extensions:
            if ext.getComponentByPosition(0) == univ.ObjectIdentifier((1,3,6,1,4,1,11129,2,4,2)):
                sct =  ext.getComponentByPosition(2)
        if sct != None:
            with self.lock:
                print self.cert_nss[0].make_ca_nickname()
                cert_sct.ParseFromString(sct)
                print 'Signed Certificate Timestamp found ' + sct
        else:
            s = self.ocsp.check_certificate_transparency()
            if s != None:
                with self.lock:
                    print s


    def verify_dnssec_tlsa(self):
        """
        DNNSEC/TLSA

        We retrieve the TLSA record of the domain and compare both fingerprints. If they are equal everything OK
        If not it could be a possible attack. Now perhaps isn't realistic because isn't widely deployed and some sites
        does not update the TLSA record for its domain. This need maybe more research
        """
        import dns.resolver
        import hashlib

        def verify(url):
            try:
                #print '_443._tcp. + url
                answer = dns.resolver.query('_443._tcp.' + url, 'TLSA')
                answer = [str(ans) for ans in answer][0].split(' ')
                hash_tlsa = answer[len(answer) - 1]
                s = hashlib.new('sha256')
                s.update(self.certs[0])
                res = s.hexdigest()
                if res == hash_tlsa:
                    return True
                else:
                    return False
            except:
                return False
                # pass

        try:
            url = self.cert_nss[0].subject_common_name
        except IndexError:
            return


        # Here I test different url because some site maybe implements dnssec without the wwww for example.
        # the site https://hacklab.to/ when you see its certificate the subject_common_name is www.hacklab.to
        # but the dnssec only respond when you ask for hacklab.to. So I have to test with different url to ensure
        # all the posibilities and provide better solution.

        # Site where you can test this verification
        #   - https://www.huque.com/  -> Valid TLSA Record
        #   - https://hacklab.to/  -> Not Valid TLSA Record

        result = False
        cert = self.cert_nss[0]
        result = verify(url)

        if result == True:
            with self.lock:
                print colored('The certificate %s with id %s has a valid TLSA record' % (cert.make_ca_nickname(), cert.serial_number), 'magenta')
            return
        if url[0:3] == "www":
            url = url.replace("www.",'')
            result = verify(url)
        elif url[0] == '*':
            url = url.replace('*', 'www')
            result = verify(url)
        if result == True:
                with self.lock:
                    print colored('The certificate %s with id %s has a valid TLSA record' % (cert.make_ca_nickname(), cert.serial_number), 'magenta')
                return 
        with self.lock:
            print colored('The certificate %s with id %s has not a valid TLSA record or not implement DANE/DNSSEC' % (cert.make_ca_nickname(), cert.serial_number), 'white')

    def verify_ocsp(self):
        """
        We check the OCSP status of our certificate

        Visit this domain : https://testssl-revoked-r2i2.disig.sk/index.en.html

        This software if it is running in Mac OS X will show you a notification seeing that the site is revoked.
        In Chrome with the default settings everything is OK. I have not find a way to enable OCSP in chrome
        """
        status, certId = self.ocsp.check_ocsp()
        name = self.cert_nss[0].make_ca_nickname()
        if status == None:
            with self.lock:
                print colored('The certificate %s with id  %s does not have OCSP URI' % (name, certId),'white')
            return
        if status == 'revoked':
            self._notify_mitm(title='OCSP-MITM')
            with self.lock:
                print colored('This certificate %s with id  %s is revoked' % (name, certId),'red')
                self._log_fail()
        else:
            with self.lock:
                print colored('This certificate %s with id %s is not revoked' % (name, certId),'cyan')

    def verify_cert_through_rfc(self):
            """
            This function try verify the certificate through RFC especification. We are using NSS to do it
            """
            approved_usage = not intended_usage
            try:
                # Turns on OCSP checking for the given certificate database.
                nss.enable_ocsp_checking(self.certdb)
                cert = self.cert_nss[0]
                ca_name = cert.make_ca_nickname()
                # Verify a certificate by checking if it's valid and if we trust the issuer. Here we are validating our certificate for SSLServer 
                approved_usage = cert.verify_now(self.certdb,True,intended_usage,None)

            except NSPRError:
                #Error ocurred maybe is due to a missed intermediate certificate so we should added but without confidence on it
                length = len(self.certs)
                self._add_certiticate_to_nssdb(1,name=cert.issuer.common_name)
                if length == 4:
                    inter = self.cert_nss[1]
                    self._add_certiticate_to_nssdb(2,name=inter.issuer.common_name)
                try:
                    approved_usage = cert.verify_now(self.certdb,True, intended_usage, None)
                except NSPRError:
                    pass

            serial = str(cert.serial_number)

            if approved_usage & intended_usage:
                with self.lock:
                    print colored('This certificate %s is safe through the RFC process ' % (serial + ' - ' + ca_name),'green')
            else:
                with self.lock:
                    print colored('This certificate %s is not safe through the RFC process ' % (serial + ' - ' + ca_name),'red')
                self._log_fail()
                self._notify_mitm(title='RFC-MITM')


    def verify_cert_with_pinning(self):
        """
        We pin our certificates each time that we see it. 
        """
        from Crypto.Util.asn1 import DerSequence
        import sha3
        s = hashlib.new("sha3_512")
        try:
            # We extract SubjectPublicKeyInfo

            der = self.certs[0]
            cert_dec = DerSequence()
            cert_dec.decode(der)
            tbsCertificate = DerSequence()
            tbsCertificate.decode(cert_dec[0])
            spki = tbsCertificate[6]

            s.update(spki)
            hash_t = s.hexdigest()
            cert = self.cert_nss[0]
            serial = cert.serial_number
            _id = str(serial) + ' - ' + cert.make_ca_nickname()
            exist = self.db_pin.get(_id)
            with self.lock:
                print hash_t
            if exist == None:
                # That means that the certificate is not in the database, it's the first time it was seen
                self.db_pin.set_pin(hash_t, _id)
                cad = "%s first seen" % _id
                with self.lock:
                    print colored(cad,'yellow')
            else:
                # Exist so we have to ensure it's correct
                correct = self.db_pin.compare(_id, hash_t)
                if correct == False:
                    cad = 'This certificate %s changed' % _id
                    with self.lock:
                        print colored(cad,'red')
                    self._notify_mitm(title=_id)
                    self._log_fail()

                else:
                    cad = 'Nothing changed ' + _id
                    with self.lock:
                        print colored(cad,'yellow')
        except Exception:
            pass


    def verify_cert_with_icsi_notary(self):

        """
        ICSI_NOTARY is used to know the "fame" of a certificate. It Provides a means to know if a certificate
        is widely seen. But it doesn't mean we are under MITM attack
        """

        import dns
        from dns import resolver
        cert = self.cert_nss[0]
        s = hashlib.new("sha1")
        s.update(self.certs[0])
        address = s.hexdigest()+'.notary.icsi.berkeley.edu'
        try:
            result =  resolver.query(address,rdtype=dns.rdatatype.TXT)[0].__str__().split()
        except:
            #icsi_notary doesn't have that certificate
            with self.lock:
                print "icsi notary does not have that certificate"
            return
        validated = int(result[4].split('=')[1][0])
        first_seen = int(result[1].split('=')[1])
        last_seen = int(result[2].split('=')[1])
        times_seen = int(result[3].split('=')[1])
        if validated is not 1:
            cad = "This certificate %s is NOT safe through icsi_notary" % (cert.make_ca_nickname())
            with self.lock:
                print colored(cad,'red')
        else:
            s = last_seen - first_seen  + 1
            if s - times_seen >= cfg.icsi.maximum_interval:
                cad = "This certificate %s is not ENOUGH secure according to icsi_notary" % (cert.make_ca_nickname())
                self._notify_mitm(title='ICSI-MITM')
                with self.lock:
                    print colored(cad,'red')
            else:
                cad = "This certificate %s IS SECURE through icsi_notary" % (cert.make_ca_nickname())
                with self.lock:
                    print colored(cad,'blue')


    def _log_fail(self):
            cn_cert = self.cert_nss[0]
            name = cn_cert.make_ca_nickname()
            exist = self.db_log.get(name)
            if exist is None:
                self.db_log.set_rfc(cn_cert.make_ca_nickname())
                # print cn_cert
                logging.info("You don't trust in this certificate when you connected to %s \n %s",
                                name, cn_cert)

    def _notify_mitm(self,title):
        if system == "Darwin":
            Notifier.notify("MITM",title=title)
        else:
            pass

    def _add_certiticate_to_nssdb(self,cert,name=None):
        """
        This function add intermediate certificate to NSS-DB only to verify. We don't trust on it
        """

        if not os.path.exists(os.getcwd() +'/tmp'):
            os.mkdir(os.getcwd() + '/tmp')

        with tempfile.NamedTemporaryFile(dir=os.getcwd() +'/tmp',suffix='crt') as tmp:
            try:
                tmp.write(M2Crypto.X509.load_cert_string(self.certs[cert],FORMAT_DER).as_pem())
            except:
                return
            tmp.flush
            tmp.seek(0)
            subprocess.call(["certutil", "-A","-n",name,'-t',',,,','-a','-i',tmp.name,'-d',self.certdb_dir])
        return



