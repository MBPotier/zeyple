#!/usr/bin/python3
# -*- coding: utf-8 -*-

from configparser import ConfigParser
import copy
import email
import email.encoders
import email.mime.application
import email.mime.multipart
import gpg
import logging
import os
import re
import smtplib
import sys


def encode_string(string):
    if isinstance(string, bytes):
        return string
    else:
        return string.encode('utf-8')


__title__ = 'Zeyple'
__version__ = '2.0.0'
__author__ = 'Cédric Félizard'
__license__ = 'AGPLv3+'
__copyright__ = 'Copyright 2012-2024 Cédric Félizard'


class Zeyple:
    """Zeyple Encrypts Your Precious Log Emails"""

    def __init__(self, config_fname='zeyple.conf'):
        self.config = self.load_configuration(config_fname)

        if self.config.has_option('zeyple', 'log_file'):
            log_file = self.config.get('zeyple', 'log_file')
            logging.basicConfig(
                filename=log_file, level=logging.DEBUG,
                format='%(asctime)s %(process)s %(levelname)s %(message)s'
            )
            logging.info("Zeyple ready to encrypt outgoing emails")
        else:
            logging.disable()

    def load_configuration(self, filename):
        """Reads and parses the config file"""

        config = ConfigParser()
        config.read([
            os.path.join('/etc/', filename),
            filename,
        ])
        if not config.sections():
            raise IOError('Cannot open config file.')
        return config

    @property
    def gpg(self):
        protocol = gpg.constants.PROTOCOL_OpenPGP

        if self.config.has_option('gpg', 'executable'):
            executable = self.config.get('gpg', 'executable')
        else:
            executable = None  # Default value

        home_dir = self.config.get('gpg', 'home')

        ctx = gpg.Context()
        ctx.set_engine_info(protocol, executable, home_dir)
        ctx.armor = True

        return ctx

    def process_message(self, message_data, recipients):
        """Encrypts the message with recipient keys"""
        message_data = encode_string(message_data)

        in_message = email.message_from_bytes(message_data)
        logging.info(
            "Processing outgoing message %s", in_message['Message-id'])

        if not recipients:
            logging.warn("Cannot find any recipients, ignoring")

        sent_messages = []
        for recipient in recipients:
            logging.info("Recipient: %s", recipient)

            key_id = self._user_key(recipient)
            logging.info("Key ID: %s", key_id)

            if key_id:
                out_message = self._encrypt_message(in_message, key_id)

            elif self.config.has_option('zeyple', 'force_encrypt') and \
                    self.config.getboolean('zeyple', 'force_encrypt'):
                logging.error("No keys found, message will not be sent!")
                continue

            else:
                logging.warn("No keys found, message will be sent unencrypted")
                out_message = copy.copy(in_message)

            self._add_zeyple_header(out_message)
            self._send_message(out_message, recipient)
            sent_messages.append(out_message)

        return sent_messages

    def _get_version_part(self):
        ret = email.mime.application.MIMEApplication(
            'Version: 1\n',
            'pgp-encrypted',
            email.encoders.encode_noop,
        )
        ret.add_header(
            'Content-Description',
            "PGP/MIME version identification",
        )
        del ret['MIME-Version']
        return ret

    def _get_encrypted_part(self, payload):
        ret = email.mime.application.MIMEApplication(
            payload,
            'octet-stream',
            email.encoders.encode_noop,
            name="encrypted.asc",
        )
        ret.add_header('Content-Description', "OpenPGP encrypted message")
        ret.add_header(
            'Content-Disposition',
            'inline',
            filename='encrypted.asc',
        )
        del ret['MIME-Version']
        return ret

    def _encrypt_message(self, in_message, key_id):
        if in_message.is_multipart():
            # get the body (after the first \n\n)
            payload = in_message.as_string().split("\n\n", 1)[1].strip()

            # prepend the Content-Type including the boundary
            content_type = "Content-Type: " + in_message["Content-Type"]
            payload = content_type + "\n\n" + payload

            message = email.message.Message()
            message.set_payload(payload)

            payload = message.get_payload()

        else:
            message = email.mime.nonmultipart.MIMENonMultipart(
                in_message.get_content_maintype(),
                in_message.get_content_subtype()
            )
            payload = encode_string(in_message.get_payload())
            message.set_payload(payload)

            # list of additional parameters in content-type
            params = in_message.get_params()
            if params:
                # first item is the main/sub type so discard it
                del params[0]
                for param, value in params:
                    message.set_param(param, value, "Content-Type", False)

            encoding = in_message["Content-Transfer-Encoding"]
            if encoding:
                message.add_header("Content-Transfer-Encoding", encoding)

            del message['MIME-Version']

            mixed = email.mime.multipart.MIMEMultipart(
                'mixed',
                None,
                [message],
            )

            # remove superfluous header
            del mixed['MIME-Version']

            payload = mixed.as_bytes()

        encrypted_payload = self._encrypt_payload(payload, [key_id])

        version = self._get_version_part()
        encrypted = self._get_encrypted_part(encrypted_payload)

        out_message = copy.copy(in_message)
        out_message.preamble = "This is an OpenPGP/MIME encrypted " \
                               "message (RFC 4880 and 3156)"

        if 'Content-Type' not in out_message:
            out_message['Content-Type'] = 'multipart/encrypted'
        else:
            out_message.replace_header(
                'Content-Type',
                'multipart/encrypted',
            )
        del out_message['Content-Transfer-Encoding']
        out_message.set_param('protocol', 'application/pgp-encrypted')
        out_message.set_payload([version, encrypted])

        return out_message

    def _encrypt_payload(self, payload, key_ids):
        """Encrypts the payload with the given keys"""
        payload = encode_string(payload)

        self.gpg.armor = True

        recipient = [self.gpg.get_key(key_id) for key_id in key_ids]

        for key in recipient:
            if key.expired:
                raise gpg.errors.GPGMEError(
                    "Key with user email %s "
                    "is expired!".format(key.uids[0].email))

        (ciphertext, encresult, signresult) = self.gpg.encrypt(
            gpg.Data(string=payload),
            recipients=recipient,
            sign=False,
            always_trust=True
        )

        return ciphertext

    def _user_key(self, email):
        """Returns the GPG key for the given email address"""
        logging.info("Trying to encrypt for %s", email)

        # Check if there is a keyalias set for the email
        # and use the replacement email for retrieving keys if so 
        if self.config.has_option('keyaliases', email):
            email=self.config.get('keyaliases', email)

        # Explicit matching of email and uid.email necessary.
        # Otherwise gpg.keylist will return a list of keys
        # for searches like "n"
        for key in self.gpg.keylist(email):
            for uid in key.uids:
                if uid.email == email:
                    return key.subkeys[0].keyid

        # Strip sub addressing tag
        submail = re.sub('(\\+[^@]+)', '', email)
        if submail != email:
            return self._user_key(submail)

        return None

    def _add_zeyple_header(self, message):
        if self.config.has_option('zeyple', 'add_header') and \
           self.config.getboolean('zeyple', 'add_header'):
            message.add_header(
                'X-Zeyple',
                "processed by {0} v{1}".format(__title__, __version__)
            )

    def _send_message(self, message, recipient):
        """Sends the given message through the SMTP relay"""
        logging.info("Sending message %s", message['Message-id'])

        smtp = smtplib.SMTP(self.config.get('relay', 'host'),
                            self.config.getint('relay', 'port'))

        smtp.sendmail(message['From'], recipient, message.as_string())
        smtp.quit()

        logging.info("Message %s sent", message['Message-id'])


if __name__ == '__main__':
    recipients = sys.argv[1:]

    binary_stdin = sys.stdin.buffer
    message = binary_stdin.read()

    zeyple = Zeyple()
    zeyple.process_message(message, recipients)
