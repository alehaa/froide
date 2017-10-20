# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import re
from datetime import datetime, timedelta
import os
import zipfile
import unittest

from mock import patch

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.conf import settings
from django.core import mail
from django.utils import timezone
from django.utils.six import BytesIO
from django.test.utils import override_settings
from django.http import QueryDict

from froide.publicbody.models import PublicBody, FoiLaw
from froide.foirequest.tests import factories
from froide.foirequest.foi_mail import package_foirequest
from froide.foirequest.models import FoiRequest, FoiMessage, FoiAttachment

User = get_user_model()


class RequestTest(TestCase):

    def setUp(self):
        factories.make_world()

    def test_public_body_logged_in_request(self):
        ok = self.client.login(email='info@fragdenstaat.de', password='froide')
        self.assertTrue(ok)

        user = User.objects.get(username='sw')
        user.organization = 'ACME Org'
        user.save()

        pb = PublicBody.objects.all()[0]
        old_number = pb.number_of_requests
        post = {
            "subject": "Test-Subject",
            "body": "This is another test body with Ümläut€n",
            "law": str(pb.default_law.pk)
        }
        response = self.client.post(reverse('foirequest-make_request',
                kwargs={'publicbody_slug': pb.slug}), post)
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.filter(user=user, public_body=pb).order_by("-id")[0]
        self.assertIsNotNone(req)
        self.assertFalse(req.public)
        self.assertEqual(req.status, "awaiting_response")
        self.assertEqual(req.visibility, 1)
        self.assertEqual(old_number + 1, req.public_body.number_of_requests)
        self.assertEqual(req.title, post['subject'])
        message = req.foimessage_set.all()[0]
        self.assertIn(post['body'], message.plaintext)
        self.assertIn('\n%s\n' % user.get_full_name(), message.plaintext)
        self.assertIn('\n%s\n' % user.organization, message.plaintext)
        self.client.logout()
        response = self.client.post(reverse('foirequest-make_public',
                kwargs={"slug": req.slug}), {})
        self.assertEqual(response.status_code, 403)
        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.post(reverse('foirequest-make_public',
                kwargs={"slug": req.slug}), {})
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.published.get(id=req.id)
        self.assertTrue(req.public)
        self.assertTrue(req.messages[-1].subject.count('[#%s]' % req.pk), 1)
        self.assertTrue(req.messages[-1].subject.endswith('[#%s]' % req.pk))

    def test_public_body_new_user_request(self):
        self.client.logout()
        factories.UserFactory.create(email="dummy@example.com")
        pb = PublicBody.objects.all()[0]
        post = {"subject": "Test-Subject With New User",
                "body": "This is a test body with new user",
                "first_name": "Stefan", "last_name": "Wehrmeyer",
                "user_email": "dummy@example.com",
                "law": pb.laws.all()[0].pk}
        response = self.client.post(reverse('foirequest-make_request',
                kwargs={'publicbody_slug': pb.slug}), post)
        self.assertTrue(response.context['user_form']['user_email'].errors)
        self.assertEqual(response.status_code, 400)
        post = {"subject": "Test-Subject With New User",
                "body": "This is a test body with new user",
                "first_name": "Stefan", "last_name": "Wehrmeyer",
                "address": "TestStreet 3\n55555 Town",
                "user_email": "sw@example.com",
                "terms": "on",
                "law": str(FoiLaw.get_default_law(pb).id)}
        response = self.client.post(reverse('foirequest-make_request',
                kwargs={'publicbody_slug': pb.slug}), post)
        self.assertEqual(response.status_code, 302)
        user = User.objects.filter(email=post['user_email']).get()
        self.assertFalse(user.is_active)
        req = FoiRequest.objects.filter(user=user, public_body=pb).get()
        self.assertEqual(req.title, post['subject'])
        self.assertEqual(req.description, post['body'])
        self.assertEqual(req.status, "awaiting_user_confirmation")
        self.assertEqual(req.visibility, 0)
        message = req.foimessage_set.all()[0]
        self.assertIn(post['body'], message.plaintext)
        self.assertIn(post['body'], message.content)
        self.assertIn(post['body'], message.get_real_content())
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(mail.outbox[0].to[0], post['user_email'])
        match = re.search(r'/%d/%d/(\w+)/' % (user.pk, req.pk),
                message.body)
        self.assertIsNotNone(match)
        secret = match.group(1)
        response = self.client.get(reverse('account-confirm',
                kwargs={'user_id': user.pk,
                'secret': secret, 'request_id': req.pk}))
        req = FoiRequest.objects.get(pk=req.pk)
        mes = req.messages[0]
        mes.timestamp = mes.timestamp - timedelta(days=2)
        mes.save()
        self.assertEqual(req.status, "awaiting_response")
        self.assertEqual(req.visibility, 1)
        self.assertEqual(len(mail.outbox), 3)
        message = mail.outbox[1]
        self.assertIn('Legal Note: This mail was sent through a Freedom Of Information Portal.', message.body)
        self.assertIn(req.secret_address, message.extra_headers.get('Reply-To', ''))
        if settings.FROIDE_CONFIG['dryrun']:
            self.assertEqual(message.to[0], "%s@%s" % (req.public_body.email.replace("@", "+"), settings.FROIDE_CONFIG['dryrun_domain']))
        else:
            self.assertEqual(message.to[0], req.public_body.email)
        self.assertEqual(message.subject, '%s [#%s]' % (req.title, req.pk))
        resp = self.client.post(reverse('foirequest-set_status',
            kwargs={"slug": req.slug}))
        self.assertEqual(resp.status_code, 400)
        response = self.client.post(reverse('foirequest-set_law',
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 400)
        new_foi_email = "foi@" + pb.email.split("@")[1]
        req.add_message_from_email({
            'msgobj': None,
            'date': timezone.now() - timedelta(days=1),
            'subject': "Re: %s" % req.title,
            'body': """Message""",
            'html': None,
            'from': ("FoI Officer", new_foi_email),
            'to': [(req.user.get_full_name(), req.secret_address)],
            'cc': [],
            'resent_to': [],
            'resent_cc': [],
            'attachments': []
        }, "FAKE_ORIGINAL")
        req = FoiRequest.objects.get(pk=req.pk)
        self.assertTrue(req.awaits_classification())
        self.assertEqual(len(req.messages), 2)
        self.assertEqual(req.messages[1].sender_email, new_foi_email)
        self.assertEqual(req.messages[1].sender_public_body,
                req.public_body)
        response = self.client.get(reverse('foirequest-show',
            kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(req.status_settable)
        response = self.client.post(reverse('foirequest-set_status',
                kwargs={"slug": req.slug}),
                {"status": "invalid_status_settings_now"})
        self.assertEqual(response.status_code, 400)
        costs = "123.45"
        status = "awaiting_response"
        response = self.client.post(reverse('foirequest-set_status',
                kwargs={"slug": req.slug}),
                {"status": status, "costs": costs})
        req = FoiRequest.objects.get(pk=req.pk)
        self.assertEqual(req.costs, float(costs))
        self.assertEqual(req.status, status)
        # send reply
        old_len = len(mail.outbox)
        response = self.client.post(reverse('foirequest-send_message',
                kwargs={"slug": req.slug}), {})
        self.assertEqual(response.status_code, 400)
        post = {"message": "My custom reply"}
        response = self.client.post(reverse('foirequest-send_message',
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 400)
        post["to"] = 'abc'
        response = self.client.post(reverse('foirequest-send_message',
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 400)
        post["to"] = '9' * 10
        response = self.client.post(reverse('foirequest-send_message',
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 400)
        post["subject"] = "Re: Custom subject"
        post["to"] = str(list(req.possible_reply_addresses().values())[0].id)
        response = self.client.post(reverse('foirequest-send_message',
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 302)
        new_len = len(mail.outbox)
        self.assertEqual(old_len + 2, new_len)
        message = list(filter(lambda x: x.subject.startswith(post['subject']), mail.outbox))[-1]
        self.assertTrue(message.subject.endswith('[#%s]' % req.pk))
        self.assertTrue(message.body.startswith(post['message']))
        self.assertIn('Legal Note: This mail was sent through a Freedom Of Information Portal.', message.body)
        self.assertIn(user.address, message.body)
        self.assertIn(new_foi_email, message.to[0])
        req._messages = None
        foimessage = list(req.messages)[-1]
        req = FoiRequest.objects.get(pk=req.pk)
        self.assertEqual(req.last_message, foimessage.timestamp)
        self.assertEqual(foimessage.recipient_public_body, req.public_body)
        self.assertTrue(req.law.meta)
        other_laws = req.law.combined.all()

        response = self.client.post(reverse('foirequest-set_law',
                kwargs={"slug": req.slug}), {'law': '9' * 5})
        self.assertEqual(response.status_code, 400)

        post = {"law": str(other_laws[0].pk)}
        response = self.client.post(reverse('foirequest-set_law',
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse('foirequest-show',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse('foirequest-set_law',
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 400)
        # logout
        self.client.logout()

        response = self.client.post(reverse('foirequest-set_law',
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 403)

        response = self.client.post(reverse('foirequest-send_message',
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse('foirequest-set_status',
                kwargs={"slug": req.slug}),
                {"status": status, "costs": costs})
        self.assertEqual(response.status_code, 403)
        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.post(reverse('foirequest-set_law',
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse('foirequest-send_message',
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 403)

    def test_public_body_not_logged_in_request(self):
        self.client.logout()
        pb = PublicBody.objects.all()[0]
        response = self.client.post(reverse('foirequest-make_request',
                kwargs={'publicbody_slug': pb.slug}),
                {"subject": "Test-Subject", "body": "This is a test body",
                    "user_email": "test@example.com"})
        self.assertEqual(response.status_code, 400)
        self.assertFormError(response, 'user_form', 'first_name',
                ['This field is required.'])
        self.assertFormError(response, 'user_form', 'last_name',
                ['This field is required.'])

    @unittest.skip('no longer allow create public body with request')
    def test_logged_in_request_new_public_body_missing(self):
        self.client.login(email="dummy@example.org", password="froide")
        response = self.client.post(reverse('foirequest-make_request'),
                {"subject": "Test-Subject", "body": "This is a test body",
                "publicbody": "new"})
        self.assertEqual(response.status_code, 400)
        self.assertFormError(response, 'public_body_form', 'name',
                ['This field is required.'])
        self.assertFormError(response, 'public_body_form', 'email',
                ['This field is required.'])
        self.assertFormError(response, 'public_body_form', 'url',
                ['This field is required.'])

    @unittest.skip('no longer allow create public body with request')
    def test_logged_in_request_new_public_body(self):
        self.client.login(email="dummy@example.org", password="froide")
        post = {"subject": "Another Test-Subject",
                "body": "This is a test body",
                "publicbody": "new",
                "public": "on",
                "law": str(settings.FROIDE_CONFIG['default_law']),
                "name": "Some New Public Body",
                "email": "public.body@example.com",
                "url": "http://example.com/public/body/"}
        response = self.client.post(
                reverse('foirequest-make_request'), post)
        self.assertEqual(response.status_code, 302)
        pb = PublicBody.objects.filter(name=post['name']).get()
        self.assertEqual(pb.url, post['url'])
        req = FoiRequest.objects.get(public_body=pb)
        self.assertEqual(req.status, "awaiting_publicbody_confirmation")
        self.assertEqual(req.visibility, 2)
        self.assertTrue(req.public)
        self.assertFalse(req.messages[0].sent)
        self.client.logout()
        # Confirm public body via admin interface
        response = self.client.post(reverse('publicbody-confirm'),
                {"publicbody": pb.pk})
        self.assertEqual(response.status_code, 403)
        # login as not staff
        self.client.login(email='dummy@example.org', password='froide')
        response = self.client.post(reverse('publicbody-confirm'),
                {"publicbody": pb.pk})
        self.assertEqual(response.status_code, 403)
        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.post(reverse('publicbody-confirm'),
                {"publicbody": "argh"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(reverse('publicbody-confirm'))
        self.assertEqual(response.status_code, 400)
        response = self.client.post(reverse('publicbody-confirm'),
                {"publicbody": "9" * 10})
        self.assertEqual(response.status_code, 404)
        response = self.client.post(reverse('publicbody-confirm'),
                {"publicbody": pb.pk})
        self.assertEqual(response.status_code, 302)
        pb = PublicBody.objects.get(id=pb.id)
        req = FoiRequest.objects.get(id=req.id)
        self.assertTrue(pb.confirmed)
        self.assertTrue(req.messages[0].sent)
        message_count = len(list(filter(
                lambda x: req.secret_address in x.extra_headers.get('Reply-To', ''),
                mail.outbox)))
        self.assertEqual(message_count, 1)
        # resent
        response = self.client.post(reverse('publicbody-confirm'),
                {"publicbody": pb.pk})
        self.assertEqual(response.status_code, 302)
        message_count = len(list(filter(
                lambda x: req.secret_address in x.extra_headers.get('Reply-To', ''),
                mail.outbox)))
        self.assertEqual(message_count, 1)

    def test_logged_in_request_with_public_body(self):
        pb = PublicBody.objects.all()[0]
        self.client.login(email="dummy@example.org", password="froide")
        post = {"subject": "Another Third Test-Subject",
                "body": "This is another test body",
                "publicbody": 'bs',
                "public": "on"}
        response = self.client.post(
                reverse('foirequest-make_request'), post)
        self.assertEqual(response.status_code, 400)
        post['law'] = str(pb.default_law.pk)
        response = self.client.post(
                reverse('foirequest-make_request'), post)
        self.assertEqual(response.status_code, 400)
        post['publicbody'] = '9' * 10  # not that many in fixture
        response = self.client.post(
                reverse('foirequest-make_request'), post)
        self.assertEqual(response.status_code, 400)
        post['publicbody'] = str(pb.pk)
        response = self.client.post(
                reverse('foirequest-make_request'), post)
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(title=post['subject'])
        self.assertEqual(req.public_body.pk, pb.pk)
        self.assertTrue(req.messages[0].sent)
        self.assertEqual(req.law, pb.default_law)

        messages = list(filter(
                lambda x: req.secret_address in x.extra_headers.get('Reply-To', ''),
                mail.outbox))
        self.assertEqual(len(messages), 1)
        message = messages[0]
        if settings.FROIDE_CONFIG['dryrun']:
            self.assertEqual(message.to[0], "%s@%s" % (
                pb.email.replace("@", "+"), settings.FROIDE_CONFIG['dryrun_domain']))
        else:
            self.assertEqual(message.to[0], pb.email)
        self.assertEqual(message.subject, '%s [#%s]' % (req.title, req.pk))

    def test_redirect_after_request(self):
        response = self.client.get(
                reverse('foirequest-make_request') + '?redirect=/speci4l-url/?blub=bla')
        self.assertContains(response, 'value="/speci4l-url/?blub=bla"')

        pb = PublicBody.objects.all()[0]
        self.client.login(email="dummy@example.org", password="froide")
        post = {"subject": "Another Third Test-Subject",
                "body": "This is another test body",
                "redirect_url": "/?blub=bla",
                "publicbody": str(pb.pk),
                "law": str(pb.default_law.pk),
                "public": "on"}
        response = self.client.post(
                reverse('foirequest-make_request'), post)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].endswith('/?blub=bla'))

        post = {"subject": "Another fourth Test-Subject",
                "body": "This is another test body",
                "redirect_url": "http://evil.example.com",
                "publicbody": str(pb.pk),
                "law": str(pb.default_law.pk),
                "public": "on"}
        response = self.client.post(
                reverse('foirequest-make_request'), post)
        req = FoiRequest.objects.get(title=post['subject'])
        self.assertIn(req.get_absolute_url(), response['Location'])

    def test_foi_email_settings(self):
        pb = PublicBody.objects.all()[0]
        self.client.login(email="dummy@example.org", password="froide")
        post = {"subject": "Another Third Test-Subject",
                "body": "This is another test body",
                "publicbody": str(pb.pk),
                'law': str(pb.default_law.pk),
                "public": "on"}
        email_func = lambda username, secret: 'email+%s@foi.example.com' % username
        with self.settings(
            FOI_EMAIL_FIXED_FROM_ADDRESS=False,
            FOI_EMAIL_TEMPLATE=email_func
        ):
            response = self.client.post(
                    reverse('foirequest-make_request'), post)
            self.assertEqual(response.status_code, 302)
            req = FoiRequest.objects.get(title=post['subject'])
            self.assertTrue(req.messages[0].sent)
            addr = email_func(req.user.username, '')
            self.assertEqual(req.secret_address, addr)

    @unittest.skip('No longer no public body')
    def test_logged_in_request_no_public_body(self):
        self.client.login(email="dummy@example.org", password="froide")
        post = {"subject": "An Empty Public Body Request",
                "body": "This is another test body",
                "law": str(FoiLaw.get_default_law().id),
                "publicbody": '',
                "public": "on"}
        response = self.client.post(
                reverse('foirequest-make_request'), post)
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(title=post['subject'])
        response = self.client.get(req.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        message = req.foimessage_set.all()[0]
        law = FoiLaw.get_default_law()
        self.assertIn(law.letter_start, message.plaintext)
        self.assertIn(law.letter_end, message.plaintext)

        # suggest public body
        other_req = FoiRequest.objects.filter(public_body__isnull=False)[0]
        for pb in PublicBody.objects.all():
            if law not in pb.laws.all():
                break
        assert FoiLaw.get_default_law(pb) != law
        response = self.client.post(
                reverse('foirequest-suggest_public_body',
                kwargs={"slug": req.slug + "garbage"}),
                {"publicbody": str(pb.pk)})
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
                reverse('foirequest-suggest_public_body',
                kwargs={"slug": other_req.slug}),
                {"publicbody": str(pb.pk)})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
                reverse('foirequest-suggest_public_body',
                kwargs={"slug": req.slug}),
                {})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
                reverse('foirequest-suggest_public_body',
                kwargs={"slug": req.slug}),
                {"publicbody": "9" * 10})
        self.assertEqual(response.status_code, 400)
        self.client.logout()
        self.client.login(email="info@fragdenstaat.de", password="froide")
        mail.outbox = []
        response = self.client.post(
                reverse('foirequest-suggest_public_body',
                kwargs={"slug": req.slug}),
                {"publicbody": str(pb.pk),
                "reason": "A good reason"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual([t.public_body for t in req.publicbodysuggestion_set.all()], [pb])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to[0], req.user.email)
        response = self.client.post(
                reverse('foirequest-suggest_public_body',
                kwargs={"slug": req.slug}),
                {"publicbody": str(pb.pk),
                "reason": "A good reason"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual([t.public_body for t in req.publicbodysuggestion_set.all()], [pb])
        self.assertEqual(len(mail.outbox), 1)

        # set public body
        response = self.client.post(
                reverse('foirequest-set_public_body',
                kwargs={"slug": req.slug + "garbage"}),
                {"suggestion": str(pb.pk)})
        self.assertEqual(response.status_code, 404)
        self.client.logout()
        self.client.login(email="dummy@example.org", password="froide")
        response = self.client.post(
                reverse('foirequest-set_public_body',
                kwargs={"slug": req.slug}),
                {})
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(title=post['subject'])
        self.assertIsNone(req.public_body)

        response = self.client.post(
                reverse('foirequest-set_public_body',
                kwargs={"slug": req.slug}),
                {"suggestion": "9" * 10})
        self.assertEqual(response.status_code, 400)
        self.client.logout()
        response = self.client.post(
                reverse('foirequest-set_public_body',
                kwargs={"slug": req.slug}),
                {"suggestion": str(pb.pk)})
        self.assertEqual(response.status_code, 403)
        self.client.login(email="dummy@example.org", password="froide")
        response = self.client.post(
                reverse('foirequest-set_public_body',
                kwargs={"slug": req.slug}),
                {"suggestion": str(pb.pk)})
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(title=post['subject'])
        message = req.foimessage_set.all()[0]
        self.assertIn(req.law.letter_start, message.plaintext)
        self.assertIn(req.law.letter_end, message.plaintext)
        self.assertNotEqual(req.law, law)
        self.assertEqual(req.public_body, pb)
        response = self.client.post(
                reverse('foirequest-set_public_body',
                kwargs={"slug": req.slug}),
                {"suggestion": str(pb.pk)})
        self.assertEqual(response.status_code, 400)

    def test_postal_reply(self):
        self.client.login(email='info@fragdenstaat.de', password='froide')
        pb = PublicBody.objects.all()[0]
        post = {
            "subject": "Totally Random Request",
            "body": "This is another test body",
            "publicbody": str(pb.pk),
            "law": str(pb.default_law.pk),
            "public": "on"
        }
        response = self.client.post(
                reverse('foirequest-make_request'), post)
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(title=post['subject'])
        response = self.client.get(reverse("foirequest-show",
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 200)
        # Date message back
        message = req.foimessage_set.all()[0]
        message.timestamp = timezone.utc.localize(
            datetime(2011, 1, 1, 0, 0, 0))
        message.save()

        file_size = os.path.getsize(factories.TEST_PDF_PATH)
        post = QueryDict(mutable=True)
        post.update({
            "reply-date": "3000-01-01",  # far future
            "reply-sender": "Some Sender",
            "reply-subject": "",
            "reply-text": "Some Text",
        })

        self.client.logout()
        response = self.client.post(reverse("foirequest-add_postal_reply",
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 403)
        self.client.login(email="info@fragdenstaat.de", password="froide")

        pb = req.public_body
        req.public_body = None
        req.save()
        response = self.client.post(reverse("foirequest-add_postal_reply",
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 400)
        req.public_body = pb
        req.save()

        response = self.client.post(reverse("foirequest-add_postal_reply",
                kwargs={"slug": req.slug}), post)
        self.assertEqual(response.status_code, 400)
        post['reply-date'] = "01/41garbl"
        response = self.client.post(reverse("foirequest-add_postal_reply",
                kwargs={"slug": req.slug}), post)
        self.assertIn('postal_reply_form', response.context)
        self.assertEqual(response.status_code, 400)
        post['reply-date'] = "2011-01-02"
        post['reply-publicbody'] = str(pb.pk)
        with open(factories.TEST_PDF_PATH, "rb") as f:
            post['reply-files'] = f
            response = self.client.post(reverse("foirequest-add_postal_reply",
                    kwargs={"slug": req.slug}), post)

        self.assertEqual(response.status_code, 302)

        message = req.foimessage_set.all()[1]

        attachment = message.foiattachment_set.all()[0]
        self.assertEqual(attachment.file.size, file_size)
        self.assertEqual(attachment.size, file_size)
        self.assertEqual(attachment.name, 'test.pdf')

        # Change name in order to upload it again
        attachment.name = 'other_test.pdf'
        attachment.save()

        postal_attachment_form = message.get_postal_attachment_form()
        self.assertTrue(postal_attachment_form)

        post = QueryDict(mutable=True)

        with open(factories.TEST_PDF_PATH, "rb") as f:
            post.update({'files': f})
            response = self.client.post(reverse('foirequest-add_postal_reply_attachment',
                kwargs={"slug": req.slug, "message_id": "9" * 5}), post)

        self.assertEqual(response.status_code, 404)

        self.client.logout()
        with open(factories.TEST_PDF_PATH, "rb") as f:
            post.update({'files': f})
            response = self.client.post(reverse('foirequest-add_postal_reply_attachment',
                kwargs={"slug": req.slug, "message_id": message.pk}), post)
        self.assertEqual(response.status_code, 403)

        self.client.login(email="dummy@example.org", password="froide")
        with open(factories.TEST_PDF_PATH, "rb") as f:
            post.update({'files': f})
            response = self.client.post(reverse('foirequest-add_postal_reply_attachment',
            kwargs={"slug": req.slug, "message_id": message.pk}), post)

        self.assertEqual(response.status_code, 403)

        self.client.logout()
        self.client.login(email='info@fragdenstaat.de', password='froide')
        message = req.foimessage_set.all()[0]

        with open(factories.TEST_PDF_PATH, "rb") as f:
            post.update({'files': f})
            response = self.client.post(reverse('foirequest-add_postal_reply_attachment',
            kwargs={"slug": req.slug, "message_id": message.pk}), post)

        self.assertEqual(response.status_code, 400)

        message = req.foimessage_set.all()[1]
        response = self.client.post(reverse('foirequest-add_postal_reply_attachment',
            kwargs={"slug": req.slug, "message_id": message.pk}))
        self.assertEqual(response.status_code, 400)

        with open(factories.TEST_PDF_PATH, "rb") as f:
            post.update({'files': f})
            response = self.client.post(reverse('foirequest-add_postal_reply_attachment',
            kwargs={"slug": req.slug, "message_id": message.pk}), post)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(message.foiattachment_set.all()), 2)

        # Adding the same document again should override the first one
        with open(factories.TEST_PDF_PATH, "rb") as f:
            post.update({'files': f})
            response = self.client.post(reverse('foirequest-add_postal_reply_attachment',
            kwargs={"slug": req.slug, "message_id": message.pk}), post)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(message.foiattachment_set.all()), 2)

    # def test_public_body_logged_in_public_request(self):
    #     ok = self.client.login(email='info@fragdenstaat.de', password='froide')
    #     user = User.objects.get(username='sw')
    #     pb = PublicBody.objects.all()[0]
    #     post = {"subject": "Test-Subject", "body": "This is a test body",
    #             "public": "on",
    #             "law": pb.default_law.pk}
    #     response = self.client.post(reverse('foirequest-make_request',
    #             kwargs={"publicbody_slug": pb.slug}), post)
    #     self.assertEqual(response.status_code, 302)

    def test_set_message_sender(self):
        from froide.foirequest.forms import MessagePublicBodySenderForm
        mail.outbox = []
        self.client.login(email="dummy@example.org", password="froide")
        pb = PublicBody.objects.all()[0]
        post = {"subject": "A simple test request",
                "body": "This is another test body",
                "law": str(pb.default_law.id),
                "publicbody": str(pb.id),
                "public": "on"}
        response = self.client.post(
                reverse('foirequest-make_request'), post)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 2)
        req = FoiRequest.objects.get(title=post['subject'])
        req.add_message_from_email({
            'msgobj': None,
            'date': timezone.now() + timedelta(days=1),
            'subject': "Re: %s" % req.title,
            'body': """Message""",
            'html': None,
            'from': ("FoI Officer", "randomfoi@example.com"),
            'to': [(req.user.get_full_name(), req.secret_address)],
            'cc': [],
            'resent_to': [],
            'resent_cc': [],
            'attachments': []
        }, "FAKE_ORIGINAL")
        req = FoiRequest.objects.get(title=post['subject'])
        self.assertEqual(len(req.messages), 2)
        self.assertEqual(len(mail.outbox), 3)
        notification = mail.outbox[-1]
        match = re.search(r'https?://[^/]+(/.*?/%d/[^\s]+)' % req.user.pk,
                notification.body)
        self.assertIsNotNone(match)
        url = match.group(1)
        self.client.logout()
        response = self.client.get(reverse('account-show'))
        self.assertEqual(response.status_code, 302)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        message = req.messages[1]
        self.assertIn(req.get_absolute_short_url(), response['Location'])
        response = self.client.get(reverse('account-show'))
        self.assertEqual(response.status_code, 200)
        form = MessagePublicBodySenderForm(message)
        post_var = form.add_prefix("sender")
        self.assertTrue(message.is_response)
        alternate_pb = PublicBody.objects.all()[1]
        response = self.client.post(
                reverse('foirequest-set_message_sender',
                kwargs={"slug": req.slug, "message_id": "9" * 8}),
                {post_var: alternate_pb.id})
        self.assertEqual(response.status_code, 404)
        self.assertNotEqual(message.sender_public_body, alternate_pb)

        self.client.logout()
        response = self.client.post(
                reverse('foirequest-set_message_sender',
                kwargs={"slug": req.slug, "message_id": str(message.pk)}),
                {post_var: alternate_pb.id})
        self.assertEqual(response.status_code, 403)
        self.assertNotEqual(message.sender_public_body, alternate_pb)

        self.client.login(email="info@fragdenstaat.de", password="froide")
        response = self.client.post(
                reverse('foirequest-set_message_sender',
                kwargs={"slug": req.slug, "message_id": str(message.pk)}),
                {post_var: alternate_pb.id})
        self.assertEqual(response.status_code, 403)
        self.assertNotEqual(message.sender_public_body, alternate_pb)

        self.client.logout()
        self.client.login(email="dummy@example.org", password="froide")
        mes = req.messages[0]
        response = self.client.post(
                reverse('foirequest-set_message_sender',
                kwargs={"slug": req.slug, "message_id": str(mes.pk)}),
                {post_var: str(alternate_pb.id)})
        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(message.sender_public_body, alternate_pb)

        response = self.client.post(
                reverse('foirequest-set_message_sender',
                kwargs={"slug": req.slug,
                    "message_id": message.pk}),
                {post_var: "9" * 5})
        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(message.sender_public_body, alternate_pb)

        response = self.client.post(
                reverse('foirequest-set_message_sender',
                kwargs={"slug": req.slug,
                    "message_id": message.pk}),
                {post_var: str(alternate_pb.id)})
        self.assertEqual(response.status_code, 302)
        message = FoiMessage.objects.get(pk=message.pk)
        self.assertEqual(message.sender_public_body, alternate_pb)

    def test_mark_not_foi(self):
        req = FoiRequest.objects.all()[0]
        self.assertTrue(req.is_foi)
        response = self.client.post(reverse('foirequest-mark_not_foi',
                kwargs={"slug": req.slug + "-blub"}))
        self.assertEqual(response.status_code, 404)

        response = self.client.post(reverse('foirequest-mark_not_foi',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 403)

        self.client.login(email="dummy@example.org", password="froide")
        response = self.client.post(reverse('foirequest-mark_not_foi',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 403)

        req = FoiRequest.objects.get(pk=req.pk)
        self.assertTrue(req.is_foi)
        self.client.logout()
        self.client.login(email="info@fragdenstaat.de", password="froide")
        response = self.client.post(reverse('foirequest-mark_not_foi',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(pk=req.pk)
        self.assertFalse(req.is_foi)

    def test_mark_checked(self):
        req = FoiRequest.objects.all()[0]
        req.checked = False
        req.save()
        self.assertFalse(req.checked)
        response = self.client.post(reverse('foirequest-mark_checked',
                kwargs={"slug": req.slug + "-blub"}))
        self.assertEqual(response.status_code, 404)

        response = self.client.post(reverse('foirequest-mark_checked',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 403)

        self.client.login(email="dummy@example.org", password="froide")
        response = self.client.post(reverse('foirequest-mark_checked',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 403)

        req = FoiRequest.objects.get(pk=req.pk)
        self.assertFalse(req.checked)
        self.client.logout()
        self.client.login(email="info@fragdenstaat.de", password="froide")
        response = self.client.post(reverse('foirequest-mark_checked',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(pk=req.pk)
        self.assertTrue(req.checked)

    def test_escalation_message(self):
        req = FoiRequest.objects.all()[0]
        zip_bytes = package_foirequest(req)
        req._messages = None  # Reset messages cache
        response = self.client.post(reverse('foirequest-escalation_message',
                kwargs={"slug": req.slug + 'blub'}))
        self.assertEqual(response.status_code, 404)
        response = self.client.post(reverse('foirequest-escalation_message',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 403)
        ok = self.client.login(email="dummy@example.org", password="froide")
        self.assertTrue(ok)
        response = self.client.post(reverse('foirequest-escalation_message',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 403)
        self.client.logout()
        self.client.login(email="info@fragdenstaat.de", password="froide")
        response = self.client.post(reverse('foirequest-escalation_message',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 400)
        mail.outbox = []
        response = self.client.post(reverse('foirequest-escalation_message',
                kwargs={"slug": req.slug}), {
                    'subject': 'My Escalation Subject',
                    'message': 'My Escalation Message'
                }
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(req.get_absolute_url(), response['Location'])
        self.assertEqual(req.law.mediator, req.messages[-1].recipient_public_body)
        self.assertEqual(len(mail.outbox), 2)
        message = list(filter(lambda x: x.to[0] == req.law.mediator.email, mail.outbox))[-1]
        self.assertEqual(message.attachments[0][0], 'request_%s.zip' % req.pk)
        self.assertEqual(message.attachments[0][2], 'application/zip')
        self.assertEqual(zipfile.ZipFile(BytesIO(message.attachments[0][1]), 'r').namelist(),
                         zipfile.ZipFile(BytesIO(zip_bytes), 'r').namelist())

    def test_set_tags(self):
        req = FoiRequest.objects.all()[0]

        # Bad method
        response = self.client.get(reverse('foirequest-set_tags',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 405)

        # Bad slug
        response = self.client.post(reverse('foirequest-set_tags',
                kwargs={"slug": req.slug + 'blub'}))
        self.assertEqual(response.status_code, 404)

        # Not logged in
        self.client.logout()
        response = self.client.post(reverse('foirequest-set_tags',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 403)

        # Not staff
        self.client.login(email='dummy@example.org', password='froide')
        response = self.client.post(reverse('foirequest-set_tags',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 403)

        # Bad form
        self.client.logout()
        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.post(reverse('foirequest-set_tags',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(req.tags.all()), 0)

        response = self.client.post(reverse('foirequest-set_tags',
                kwargs={"slug": req.slug}),
                {'tags': 'SomeTag, "Another Tag", SomeTag'})
        self.assertEqual(response.status_code, 302)
        tags = req.tags.all()
        self.assertEqual(len(tags), 2)
        self.assertIn('SomeTag', [t.name for t in tags])
        self.assertIn('Another Tag', [t.name for t in tags])

    def test_set_summary(self):
        req = FoiRequest.objects.all()[0]

        # Bad method
        response = self.client.get(reverse('foirequest-set_summary',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 405)

        # Bad slug
        response = self.client.post(reverse('foirequest-set_summary',
                kwargs={"slug": req.slug + 'blub'}))
        self.assertEqual(response.status_code, 404)

        # Not logged in
        self.client.logout()
        response = self.client.post(reverse('foirequest-set_summary',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 403)

        # Not user of request
        self.client.login(email='dummy@example.org', password='froide')
        response = self.client.post(reverse('foirequest-set_summary',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 403)

        # Request not final
        self.client.logout()
        self.client.login(email='info@fragdenstaat.de', password='froide')
        req.status = 'awaiting_response'
        req.save()
        response = self.client.post(reverse('foirequest-set_summary',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 400)

        # No resolution given
        req.status = 'resolved'
        req.save()
        response = self.client.post(reverse('foirequest-set_summary',
                kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 400)

        res = "This is resolved"
        response = self.client.post(reverse('foirequest-set_summary',
                kwargs={"slug": req.slug}), {"summary": res})
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(id=req.id)
        self.assertEqual(req.summary, res)

    def test_approve_attachment(self):
        req = FoiRequest.objects.all()[0]
        mes = req.messages[-1]
        att = factories.FoiAttachmentFactory.create(belongs_to=mes, approved=False)

        # Bad method
        response = self.client.get(reverse('foirequest-approve_attachment',
                kwargs={"slug": req.slug, "attachment": att.id}))
        self.assertEqual(response.status_code, 405)

        # Bad slug
        response = self.client.post(reverse('foirequest-approve_attachment',
                kwargs={"slug": req.slug + 'blub', "attachment": att.id}))
        self.assertEqual(response.status_code, 404)

        # Not logged in
        self.client.logout()
        response = self.client.post(reverse('foirequest-approve_attachment',
                kwargs={"slug": req.slug, "attachment": att.id}))
        self.assertEqual(response.status_code, 403)

        # Not user of request
        self.client.login(email='dummy@example.org', password='froide')
        response = self.client.post(reverse('foirequest-approve_attachment',
                kwargs={"slug": req.slug, "attachment": att.id}))
        self.assertEqual(response.status_code, 403)
        self.client.logout()

        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.post(reverse('foirequest-approve_attachment',
                kwargs={"slug": req.slug, "attachment": '9' * 8}))
        self.assertEqual(response.status_code, 404)

        user = User.objects.get(username='sw')
        user.is_staff = False
        user.save()

        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.post(reverse('foirequest-approve_attachment',
                kwargs={"slug": req.slug, "attachment": att.id}))
        self.assertEqual(response.status_code, 302)
        att = FoiAttachment.objects.get(id=att.id)
        self.assertTrue(att.approved)

        att.approved = False
        att.can_approve = False
        att.save()
        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.post(reverse('foirequest-approve_attachment',
                kwargs={"slug": req.slug, "attachment": att.id}))
        self.assertEqual(response.status_code, 403)
        att = FoiAttachment.objects.get(id=att.id)
        self.assertFalse(att.approved)
        self.assertFalse(att.can_approve)

        self.client.logout()
        self.client.login(email='dummy_staff@example.org', password='froide')
        response = self.client.post(reverse('foirequest-approve_attachment',
                kwargs={"slug": req.slug, "attachment": att.id}))
        self.assertEqual(response.status_code, 302)
        att = FoiAttachment.objects.get(id=att.id)
        self.assertTrue(att.approved)
        self.assertFalse(att.can_approve)

    def test_make_same_request(self):
        fake_mes = factories.FoiMessageFactory.create(not_publishable=True)
        req = FoiRequest.objects.all()[0]
        mes = req.messages[-1]

        # req doesn't exist
        response = self.client.post(reverse('foirequest-make_same_request',
                kwargs={"slug": req.slug + 'blub', "message_id": '9' * 4}))
        self.assertEqual(response.status_code, 404)

        # message doesn't exist
        response = self.client.post(reverse('foirequest-make_same_request',
                kwargs={"slug": req.slug, "message_id": '9' * 4}))
        self.assertEqual(response.status_code, 404)

        # message is publishable
        response = self.client.post(reverse('foirequest-make_same_request',
                kwargs={"slug": req.slug, "message_id": mes.id}))
        self.assertEqual(response.status_code, 400)

        # message does not belong to request
        response = self.client.post(reverse('foirequest-make_same_request',
                kwargs={"slug": req.slug, "message_id": fake_mes.id}))
        self.assertEqual(response.status_code, 400)

        # not loged in, no form
        mes.not_publishable = True
        mes.save()

        response = self.client.get(reverse('foirequest-show', kwargs={"slug": req.slug}))
        self.assertEqual(response.status_code, 200)

        response = self.client.post(reverse('foirequest-make_same_request',
                kwargs={"slug": req.slug, "message_id": mes.id}))
        self.assertEqual(response.status_code, 400)

        # user made original request
        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.post(reverse('foirequest-make_same_request',
                kwargs={"slug": req.slug, "message_id": mes.id}))
        self.assertEqual(response.status_code, 400)

        # make request
        mail.outbox = []
        self.client.logout()
        self.client.login(email='dummy@example.org', password='froide')
        response = self.client.post(reverse('foirequest-make_same_request',
                kwargs={"slug": req.slug, "message_id": mes.id}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 2)
        user = User.objects.get(username='dummy')
        same_req = FoiRequest.objects.get(same_as=req, user=user)
        self.assertIn(same_req.get_absolute_url(), response['Location'])
        self.assertEqual(list(req.same_as_set), [same_req])
        self.assertEqual(same_req.identical_count(), 1)
        req = FoiRequest.objects.get(pk=req.pk)
        self.assertEqual(req.identical_count(), 1)

        response = self.client.post(reverse('foirequest-make_same_request',
                kwargs={"slug": req.slug, "message_id": mes.id}))
        self.assertEqual(response.status_code, 400)
        same_req = FoiRequest.objects.get(same_as=req, user=user)

        same_mes = factories.FoiMessageFactory.create(
            request=same_req, not_publishable=True)
        self.client.logout()
        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.post(reverse('foirequest-make_same_request',
                kwargs={"slug": same_req.slug, "message_id": same_mes.id}))
        self.assertEqual(response.status_code, 400)

        self.client.logout()
        mail.outbox = []
        post = {"first_name": "Bob",
                "last_name": "Bobbington",
                "address": "MyAddres 12\nB-Town",
                "user_email": "bob@example.com",
                "terms": "on"
        }
        response = self.client.post(reverse('foirequest-make_same_request',
                kwargs={"slug": same_req.slug, "message_id": same_mes.id}), post)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(FoiRequest.objects.filter(same_as=req).count(), 2)
        same_req2 = FoiRequest.objects.get(same_as=req, user__email=post['user_email'])
        self.assertEqual(same_req2.status, "awaiting_user_confirmation")
        self.assertEqual(same_req2.visibility, 0)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to[0], post['user_email'])
        match = re.search(r'/(\d+)/%d/(\w+)/' % (same_req2.pk), message.body)
        self.assertIsNotNone(match)
        new_user = User.objects.get(id=int(match.group(1)))
        self.assertFalse(new_user.is_active)
        secret = match.group(2)
        response = self.client.get(reverse('account-confirm',
                kwargs={'user_id': new_user.pk,
                'secret': secret, 'request_id': same_req2.pk}))
        self.assertEqual(response.status_code, 302)
        new_user = User.objects.get(id=new_user.pk)
        self.assertTrue(new_user.is_active)
        same_req2 = FoiRequest.objects.get(pk=same_req2.pk)
        self.assertEqual(same_req2.status, "awaiting_response")
        self.assertEqual(same_req2.visibility, 2)
        self.assertEqual(len(mail.outbox), 3)

    def test_empty_costs(self):
        req = FoiRequest.objects.all()[0]
        user = User.objects.get(username='sw')
        req.status = 'awaits_classification'
        req.user = user
        req.save()
        factories.FoiMessageFactory.create(
            status=None,
            request=req
        )
        self.client.login(email='info@fragdenstaat.de', password='froide')
        status = 'awaiting_response'
        response = self.client.post(reverse('foirequest-set_status',
                kwargs={"slug": req.slug}),
                {"status": status, "costs": "", 'resolution': ''})
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(pk=req.pk)
        self.assertEqual(req.costs, 0.0)
        self.assertEqual(req.status, status)

    def test_resolution(self):
        req = FoiRequest.objects.all()[0]
        user = User.objects.get(username='sw')
        req.status = 'awaits_classification'
        req.user = user
        req.save()
        mes = factories.FoiMessageFactory.create(
            status=None,
            request=req
        )
        self.client.login(email='info@fragdenstaat.de', password='froide')
        status = 'resolved'
        response = self.client.post(reverse('foirequest-set_status',
                kwargs={"slug": req.slug}),
                {"status": status, "costs": "", 'resolution': ''})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(reverse('foirequest-set_status',
                kwargs={"slug": req.slug}),
                {"status": status, "costs": "", 'resolution': 'bogus'})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(reverse('foirequest-set_status',
                kwargs={"slug": req.slug}),
                {"status": status, "costs": "", 'resolution': 'successful'})
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(pk=req.pk)
        self.assertEqual(req.costs, 0.0)
        self.assertEqual(req.status, 'resolved')
        self.assertEqual(req.resolution, 'successful')
        self.assertEqual(req.days_to_resolution(),
                         (mes.timestamp - req.first_message).days)

    def test_redirect(self):
        req = FoiRequest.objects.all()[0]
        user = User.objects.get(username='sw')
        req.status = 'awaits_classification'
        req.user = user
        req.save()
        factories.FoiMessageFactory.create(
            status=None,
            request=req
        )
        pb = factories.PublicBodyFactory.create()
        # old_due = req.due_date
        self.assertNotEqual(req.public_body, pb)
        self.client.login(email='info@fragdenstaat.de', password='froide')
        status = 'request_redirected'
        response = self.client.post(reverse('foirequest-set_status',
                kwargs={"slug": req.slug}),
                {"status": status, "costs": "", 'resolution': ''})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(reverse('foirequest-set_status',
                kwargs={"slug": req.slug}),
                {"status": status, "costs": "", 'redirected': '9' * 7})
        self.assertEqual(response.status_code, 400)
        # response = self.client.post(reverse('foirequest-set_status',
        #         kwargs={"slug": req.slug}),
        #         {"status": status, "costs": "", 'redirected': str(pb.pk)})
        # self.assertEqual(response.status_code, 302)
        # req = FoiRequest.objects.get(pk=req.pk)
        # self.assertEqual(req.costs, 0.0)
        # self.assertEqual(req.status, 'awaiting_response')
        # self.assertEqual(req.resolution, '')
        # self.assertEqual(req.public_body, pb)
        # self.assertNotEqual(old_due, req.due_date)

    def test_search(self):
        pb = PublicBody.objects.all()[0]
        factories.rebuild_index()
        response = self.client.get('%s?q=%s' % (
            reverse('foirequest-search'), pb.name[:6]))
        self.assertIn(pb.name, response.content.decode('utf-8'))
        self.assertEqual(response.status_code, 200)

    def test_full_text_request(self):
        self.client.login(email="dummy@example.org", password="froide")
        pb = PublicBody.objects.all()[0]
        law = pb.default_law
        post = {"subject": "A Public Body Request",
                "body": "This is another test body with Ümläut€n",
                "full_text": "true",
                "law": str(law.id),
                "publicbody": str(pb.id),
                "public": "on"}
        response = self.client.post(
                reverse('foirequest-make_request'), post)
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(title=post['subject'])
        message = req.foimessage_set.all()[0]
        self.assertIn(post['body'], message.plaintext)
        self.assertIn(post['body'], message.plaintext_redacted)
        self.assertNotIn(law.letter_start, message.plaintext)
        self.assertNotIn(law.letter_start, message.plaintext_redacted)
        self.assertNotIn(law.letter_end, message.plaintext)
        self.assertNotIn(law.letter_end, message.plaintext_redacted)

    def test_redaction_config(self):
        self.client.login(email="dummy@example.org", password="froide")
        req = FoiRequest.objects.all()[0]
        name = "Petra Radetzky"
        req.add_message_from_email({
            'msgobj': None,
            'date': timezone.now(),
            'subject': 'Reply',
            'body': ("Sehr geehrte Damen und Herren,\nblub\nbla\n\n"
                     "Mit freundlichen Grüßen\n" +
                     name),
            'html': 'html',
            'from': ('Petra Radetzky', 'petra.radetsky@bund.example.org'),
            'to': [req.secret_address],
            'cc': [],
            'resent_to': [],
            'resent_cc': [],
            'attachments': []
        }, '')
        req = FoiRequest.objects.all()[0]
        last = req.messages[-1]
        self.assertNotIn(name, last.plaintext_redacted)
        req.add_message(req.user, 'Test', 'test@example.com',
            'Testing',
            'Sehr geehrte Frau Radetzky,\n\nblub\n\nMit freundlichen Grüßen\nStefan Wehrmeyer'
        )
        req = FoiRequest.objects.all()[0]
        last = req.messages[-1]
        self.assertNotIn('Radetzky', last.plaintext_redacted)

    def test_empty_pb_email(self):
        self.client.login(email='info@fragdenstaat.de', password='froide')
        pb = PublicBody.objects.all()[0]
        pb.email = ''
        pb.save()
        post = {
            "subject": "Test-Subject",
            "body": "This is a test body",
            "law": str(pb.default_law.pk)
        }
        response = self.client.post(
            reverse('foirequest-make_request',
                kwargs={'publicbody_slug': pb.slug}
        ), post)
        self.assertEqual(response.status_code, 404)
        post = {
            "subject": "Test-Subject",
            "body": "This is a test body",
            "law": str(pb.default_law.pk),
            "publicbody": str(pb.pk),
        }
        response = self.client.post(
            reverse('foirequest-make_request'), post)
        self.assertEqual(response.status_code, 400)
        self.assertIn('publicbody', response.context['publicbody_form'].errors)
        self.assertEqual(len(response.context['publicbody_form'].errors), 1)

    @patch('froide.foirequest.views.convert_to_pdf',
           lambda x: factories.TEST_PDF_PATH)
    def test_redact_attachment(self):
        foirequest = FoiRequest.objects.all()[0]
        message = foirequest.messages[0]
        att = factories.FoiAttachmentFactory.create(belongs_to=message)
        url = reverse('foirequest-redact_attachment', kwargs={
            'slug': foirequest.slug,
            'attachment_id': '8' * 5
        })

        self.assertIn(att.name, repr(att))

        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

        url = reverse('foirequest-redact_attachment', kwargs={
            'slug': foirequest.slug,
            'attachment_id': str(att.id)
        })
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        response = self.client.post(url, {})
        self.assertEqual(response.status_code, 302)

        old_att = FoiAttachment.objects.get(id=att.id)
        self.assertFalse(old_att.can_approve)

    def test_extend_deadline(self):
        foirequest = FoiRequest.objects.all()[0]
        old_due_date = foirequest.due_date
        url = reverse('foirequest-extend_deadline', kwargs={'slug': foirequest.slug})
        post = {"months": ""}

        response = self.client.post(url, post)
        self.assertEqual(response.status_code, 403)

        self.client.login(email='dummy@example.org', password='froide')
        response = self.client.post(url, post)
        self.assertEqual(response.status_code, 403)

        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.post(url, post)
        self.assertEqual(response.status_code, 400)

        post = {'months': '2'}
        response = self.client.post(url, post)
        self.assertEqual(response.status_code, 302)
        foirequest = FoiRequest.objects.get(id=foirequest.id)
        self.assertEqual(foirequest.due_date, foirequest.law.calculate_due_date(old_due_date, 2))

    def test_resend_message(self):
        foirequest = FoiRequest.objects.all()[0]
        message = foirequest.messages[0]
        message.sent = False
        message.save()
        url = reverse('foirequest-resend_message', kwargs={'slug': foirequest.slug})
        post = {'message': ''}

        response = self.client.post(url, post)
        self.assertEqual(response.status_code, 403)

        self.client.login(email='dummy@example.org', password='froide')
        response = self.client.post(url, post)
        self.assertEqual(response.status_code, 403)

        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.post(url, post)
        self.assertEqual(response.status_code, 400)

        post = {'message': '8' * 6}
        response = self.client.post(url, post)
        self.assertEqual(response.status_code, 400)

        post = {'message': str(message.pk)}
        response = self.client.post(url, post)
        self.assertEqual(response.status_code, 302)

    def test_approve_message(self):
        foirequest = FoiRequest.objects.all()[0]
        message = foirequest.messages[0]
        message.content_hidden = True
        message.save()
        url = reverse('foirequest-approve_message', kwargs={
            'slug': foirequest.slug,
            'message': message.pk
        })

        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

        self.client.login(email='dummy@example.org', password='froide')
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)

        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

        message = FoiMessage.objects.get(pk=message.pk)
        self.assertFalse(message.content_hidden)

    def test_too_long_subject(self):
        self.client.login(email='info@fragdenstaat.de', password='froide')
        pb = PublicBody.objects.all()[0]
        post = {
            "subject": "Test" * 64,
            "body": "This is another test body with Ümläut€n",
            "law": str(pb.default_law.pk)
        }
        response = self.client.post(reverse('foirequest-make_request',
                kwargs={'publicbody_slug': pb.slug}), post)
        self.assertEqual(response.status_code, 400)

        post = {
            "subject": "Test" * 55 + ' a@b.de',
            "body": "This is another test body with Ümläut€n",
            "law": str(pb.default_law.pk)
        }
        response = self.client.post(reverse('foirequest-make_request',
                kwargs={'publicbody_slug': pb.slug}), post)
        self.assertEqual(response.status_code, 302)

    def test_remove_double_numbering(self):
        req = FoiRequest.objects.all()[0]
        req.add_message(req.user, 'Test', 'test@example.com',
            req.title + ' [#%s]' % req.pk,
            'Test'
        )
        req = FoiRequest.objects.all()[0]
        last = req.messages[-1]
        self.assertEqual(last.subject.count('[#%s]' % req.pk), 1)

    @override_settings(FOI_EMAIL_FIXED_FROM_ADDRESS=False)
    def test_user_name_phd(self):
        from froide.helper.email_utils import make_address
        from_addr = make_address('j.doe.12345@example.org', 'John Doe, Dr.')
        self.assertEqual(from_addr, '"John Doe, Dr." <j.doe.12345@example.org>')

    def test_throttling(self):
        froide_config = settings.FROIDE_CONFIG
        froide_config['request_throttle'] = [(2, 60), (5, 60 * 60)]

        pb = PublicBody.objects.all()[0]
        self.client.login(email="dummy@example.org", password="froide")

        with self.settings(FROIDE_CONFIG=froide_config):
            post = {"subject": "Another Third Test-Subject",
                    "body": "This is another test body",
                    "publicbody": str(pb.pk),
                    "public": "on"}
            post['law'] = str(pb.default_law.pk)

            response = self.client.post(
                    reverse('foirequest-make_request'), post)
            self.assertEqual(response.status_code, 302)

            response = self.client.post(
                    reverse('foirequest-make_request'), post)
            self.assertEqual(response.status_code, 302)

            response = self.client.post(
                    reverse('foirequest-make_request'), post)

            self.assertContains(response,
                'exceeded your request limit of 2 requests in 1',
                status_code=400)

    def test_throttling_same_as(self):
        froide_config = settings.FROIDE_CONFIG
        froide_config['request_throttle'] = [(2, 60), (5, 60 * 60)]

        # pb = PublicBody.objects.all()[0]
        # user = User.objects.get(username='sw')
        messages = []
        for i in range(3):
            req = factories.FoiRequestFactory(slug='same-as-request-%d' % i)
            messages.append(
                factories.FoiMessageFactory.create(
                    not_publishable=True,
                    request=req
                )
            )

        self.client.login(email="dummy@example.org", password="froide")

        with self.settings(FROIDE_CONFIG=froide_config):

            for i, mes in enumerate(messages):
                response = self.client.post(reverse('foirequest-make_same_request',
                        kwargs={"slug": mes.request.slug, "message_id": mes.id}))
                if i < 2:
                    self.assertEqual(response.status_code, 302)

            self.assertContains(response,
                "exceeded your request limit of 2 requests in 1\xa0minute.",
                status_code=400)


class MediatorTest(TestCase):
    def setUp(self):
        self.site = factories.make_world()

    def test_hiding_content(self):
        req = FoiRequest.objects.all()[0]
        mediator = req.law.mediator
        req.add_escalation_message('Escalate', 'Content')
        req = FoiRequest.objects.all()[0]
        req.add_message_from_email({
            'msgobj': None,
            'date': timezone.now(),
            'subject': 'Reply',
            'body': 'Content',
            'html': 'html',
            'from': ('Name', mediator.email),
            'to': [req.secret_address],
            'cc': [],
            'resent_to': [],
            'resent_cc': [],
            'attachments': []
        }, '')
        req = FoiRequest.objects.all()[0]
        last = req.messages[-1]
        self.assertTrue(last.content_hidden)

    def test_no_public_body(self):
        user = User.objects.get(username='sw')
        req = factories.FoiRequestFactory.create(
            user=user,
            public_body=None,
            status='public_body_needed',
            site=self.site
        )
        req.save()
        self.client.login(email='info@fragdenstaat.de', password='froide')
        response = self.client.get(req.get_absolute_url())
        self.assertNotIn('Mediation', response.content.decode('utf-8'))
        response = self.client.post(reverse('foirequest-escalation_message',
            kwargs={'slug': req.slug}))
        self.assertEqual(response.status_code, 400)
        message = list(response.context['messages'])[0]
        self.assertIn('cannot be escalated', message.message)


class JurisdictionTest(TestCase):
    def setUp(self):
        self.site = factories.make_world()
        self.pb = PublicBody.objects.filter(jurisdiction__slug='nrw')[0]

    def test_letter_public_body(self):
        self.client.login(email='info@fragdenstaat.de', password='froide')
        post = {
            "subject": "Jurisdiction-Test-Subject",
            "body": "This is a test body",
            "law": str(self.pb.default_law.pk)
        }
        response = self.client.post(
            reverse('foirequest-make_request',
                kwargs={'publicbody_slug': self.pb.slug}
        ), post)
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(title='Jurisdiction-Test-Subject')
        law = FoiLaw.objects.get(meta=True, jurisdiction__slug='nrw')
        self.assertEqual(req.law, law)
        mes = req.messages[0]
        self.assertIn(law.letter_end, mes.plaintext)

    @unittest.skip('no longer allow empty public body')
    def test_letter_set_public_body(self):
        self.client.login(email='info@fragdenstaat.de', password='froide')
        post = {
            "subject": "Jurisdiction-Test-Subject",
            "body": "This is a test body",
            'law': str(FoiLaw.get_default_law().pk),
            'publicbody': ''
        }
        response = self.client.post(
            reverse('foirequest-make_request'), post)
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(
            title=post['subject']
        )
        default_law = FoiLaw.get_default_law()
        self.assertEqual(req.law, default_law)
        mes = req.messages[0]
        self.assertIn(default_law.letter_end, mes.plaintext)
        self.assertIn(default_law.letter_end, mes.plaintext_redacted)

        response = self.client.post(
                reverse('foirequest-suggest_public_body',
                kwargs={"slug": req.slug}),
                {"publicbody": str(self.pb.pk),
                "reason": "A good reason"})
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
                reverse('foirequest-set_public_body',
                kwargs={"slug": req.slug}),
                {"suggestion": str(self.pb.pk)})
        self.assertEqual(response.status_code, 302)
        req = FoiRequest.objects.get(title=post['subject'])
        law = FoiLaw.objects.get(meta=True, jurisdiction__slug='nrw')
        self.assertEqual(req.law, law)
        mes = req.messages[0]
        self.assertNotEqual(default_law.letter_end, law.letter_end)
        self.assertIn(law.letter_end, mes.plaintext)
        self.assertIn(law.letter_end, mes.plaintext_redacted)


class PackageFoiRequestTest(TestCase):
    def setUp(self):
        factories.make_world()

    def test_package(self):
        fr = FoiRequest.objects.all()[0]
        bytes = package_foirequest(fr)
        zfile = zipfile.ZipFile(BytesIO(bytes), 'r')
        filenames = [
            r'20\d{2}-\d{2}-\d{2}_1_requester\.txt',
            r'20\d{2}-\d{2}-\d{2}_1_publicbody\.txt',
            r'20\d{2}-\d{2}-\d{2}_1-file_\d+\.pdf',
            r'20\d{2}-\d{2}-\d{2}_1-file_\d+\.pdf'
        ]
        zip_names = zfile.namelist()
        self.assertEqual(len(filenames), len(zip_names))
        for zname, fname in zip(zip_names, filenames):
            self.assertTrue(bool(re.match(r'^%s$' % fname, zname)))
