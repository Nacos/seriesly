import datetime
import logging
import vobject

from google.appengine.api import xmpp
from google.appengine.api import mail

from django.shortcuts import render_to_response
from django.http import HttpResponse,HttpResponseRedirect,Http404
from django.template import RequestContext
from django.template.loader import render_to_string
from django.conf import settings

from helper import is_post
from series.models import Show, Episode
from subscription.forms import SubscriptionForm, MailSubscriptionForm, XMPPSubscriptionForm, WebHookSubscriptionForm
from subscription.models import Subscription
from releases.models import Release

def index(request, form=None, extra_context=None):
    if form is None:
        form = SubscriptionForm()
    context = {"form" : form}
    if extra_context is not None:
        context.update(extra_context)
    return render_to_response("index.html", RequestContext(request, context))
    
@is_post
def subscribe(request):
    logging.warn(request.POST)
    form = SubscriptionForm(request.POST)
    if not form.is_valid():
        logging.warn(form.errors)
        return index(request, form=form)
    editing = False
    if form.cleaned_data["subkey"] == "":
        subkey = Subscription.generate_subkey()
        subscription = Subscription(last_changed=datetime.datetime.now(),subkey=subkey)
    else:
        editing = True
        subkey = form.cleaned_data["subkey"]
        subscription = form._subscription
    logging.warn("Torrent %s" % str(form.cleaned_data["torrent"]))
    logging.warn("Stream %s" % str(form.cleaned_data["stream"]))
    settings = {"quality": form.cleaned_data["quality"],
                "torrent": str(form.cleaned_data["torrent"]),
                "stream" : str(form.cleaned_data["stream"])
            }
    subscription.set_settings(settings)
    
    try:
        selected_shows = Show.get_by_id(map(int,form.cleaned_data["shows"]))
    except ValueError:
        return index(request, form=form)
    subscription.put()
    
    if editing:
        subscription.set_shows(selected_shows, old_shows=subscription.get_shows())
    else:
        subscription.set_shows(selected_shows)
    response = HttpResponseRedirect(subscription.get_absolute_url())
    response.set_cookie("subkey", subkey, max_age=31536000)
    return response
    
def show(request, subkey, extra_context=None):
    subscription = Subscription.all().filter("subkey =", subkey).get()
    if subscription is None:
        raise Http404
    if extra_context is None:
        extra_context = {}
    if "mail_form" in extra_context:
        subscription.mail_form = extra_context["mail_form"]
    else:
        subscription.mail_form = MailSubscriptionForm({"email": subscription.email, "subkey": subkey})
    if "xmpp_form" in extra_context:
        subscription.xmpp_form = extra_context["xmpp_form"]
    else:
        subscription.xmpp_form = XMPPSubscriptionForm({"xmpp": subscription.xmpp, "subkey": subkey})
    if "webhook_form" in extra_context:
        subscription.webhook_form = extra_context["webhook_form"]
    else:
        subscription.webhook_form = WebHookSubscriptionForm({"webhook": subscription.webhook, "subkey": subkey})
    response = render_to_response("subscription.html", RequestContext(request, {"subscription":subscription}))
    response.set_cookie("subkey", subkey, max_age=31536000)
    return response
    
@is_post
def edit_mail(request):
    form = MailSubscriptionForm(request.POST)
    if not form.is_valid():
        return show(request, request.POST.get("subkey", ""), extra_context={"mail_form":form})
    subscription = form._subscription
    if subscription.email != form.cleaned_data["email"]:
        subscription.activated_mail = False
    subscription.email = form.cleaned_data["email"]
    subscription.last_changed = datetime.datetime.now()
    subscription.put()
    if subscription.email != "" and subscription.activated_mail == False:
        subscription.send_confirmation_mail()
    return HttpResponseRedirect(subscription.get_absolute_url() + "#email-subscription")
    
def confirm_mail(request, subkey, confirmkey):
    subscription = Subscription.all().filter("subkey =", subkey).get()
    if subscription is None:
        raise Http404
    if subscription.check_confirmation_key(confirmkey):
        subscription.activated_mail = True
        subscription.put()
        return HttpResponseRedirect(subscription.get_absolute_url() + "#email-subscription")
    else:
        raise Http404
        
def edit(request, subkey):
    subscription = Subscription.all().filter("subkey =", subkey).get()
    if subscription is None:
        raise Http404
    if request.method == "GET":
        sub_settings = subscription.get_settings()
        sub_dict = {"email": subscription.email, 
                    "quality": sub_settings["quality"],
                    "torrent": sub_settings["torrent"],
                    "stream": sub_settings["stream"],
                    "shows": map(lambda x: x.idnr, subscription.get_shows()),
                    "subkey": subkey}
        form = SubscriptionForm(sub_dict)
        return index(request, form=form, extra_context={"subscription": subscription})
    return HttpResponseRedirect(subscription.get_absolute_url())
        
def feed_rss(request, subkey):
    return feed(request, subkey, template="rss.xml")
    
def feed_atom(request, subkey):
    return feed(request, subkey, template="atom.xml")
    
def feed(request, subkey, template="atom.xml"):
    subscription = Subscription.all().filter("subkey =", subkey).get()
    if subscription is None:
        raise Http404
    subscription.last_visited = datetime.datetime.now()
    sub_settings = subscription.get_settings()
    subscription.put()
    now = datetime.datetime.now()
    subscription.updated = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    subscription.expires = (now + datetime.timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    the_shows = subscription.get_shows()
    two_weeks_ago = now - datetime.timedelta(days=28)
    five_hours = datetime.timedelta(hours=5)
    episodes = Episode.get_for_shows(the_shows, before=now, order="-date")
    items = []
    for episode in episodes:
        releases = Release.filter(episode.releases, sub_settings)
        if len(releases) > 0 or now > episode.date + five_hours:
            torrenturl = False
            torrentlen = 0
            pub_date = episode.date
            if len(releases) > 0:
                # Some smart ranking needed here
                torrenturl = releases[0].url
                torrentlen = releases[0].torrentlen
                pub_date = releases[0].pub_date
            episode.torrenturl = torrenturl
            episode.torrentlen = torrentlen
            episode.pub_date = pub_date
            episode.releases = releases
            items.append(episode)
    
    body = render_to_string(template, RequestContext(request, {"subscription":subscription, "items": items}))
    return HttpResponse(body, mimetype="application/atom+xml")    
    
def calendar(request, subkey):
    """Nice hints from here: http://blog.thescoop.org/archives/2007/07/31/django-ical-and-vobject/"""
    subscription = Subscription.all().filter("subkey =", subkey).get()
    if subscription is None:
        raise Http404
    subscription.last_visited = datetime.datetime.now()
    sub_settings = subscription.get_settings()
    subscription.put()
    the_shows = subscription.get_shows()
    now = datetime.datetime.now()
    two_weeks_ago = now - datetime.timedelta(days=7)
    five_hours = datetime.timedelta(hours=5)
    episodes = Episode.get_for_shows(the_shows, order="date")
    cal = vobject.iCalendar()
    cal.add('method').value = 'PUBLISH'  # IE/Outlook needs this
    utc = vobject.icalendar.utc
    items = []
    for episode in episodes:
        releases = Release.filter(episode.releases, sub_settings)
        vevent = episode.create_event_details(cal)
        if releases:
            vevent.add('url').value = releases[0].url
            vevent.add('description').value = u"\n".join(map(unicode, releases))
    icalstream = cal.serialize()
    response = HttpResponse(icalstream, mimetype='text/calendar')
    response['Filename'] = 'seriesly-calendar.ics'  # IE needs this
    response['Content-Disposition'] = 'attachment; filename=seriesly-calendar.ics'
    return response
    
def guide(request, subkey):
    subscription = Subscription.all().filter("subkey =", subkey).get()
    if subscription is None:
        raise Http404
    subscription.last_visited = datetime.datetime.now()
    sub_settings = subscription.get_settings()
    subscription.put()
    the_shows = subscription.get_shows()
    episodes = Episode.get_for_shows(the_shows, order="date")
    now = datetime.datetime.now()
    twentyfour_hours_ago = now - datetime.timedelta(hours=24)
    recently = []
    last_week = []
    upcoming = []
    for episode in episodes:
        if episode.date < now:
            releases = Release.filter(episode.releases, sub_settings)
        else:
             releases = []
        episode.releases = releases
        if episode.date < twentyfour_hours_ago:
            last_week.append(episode)
        elif episode.date <= now:
            recently.append(episode)
        else:
            upcoming.append(episode)
    response = render_to_response("guide.html", RequestContext(request, {"subscription":subscription, 
                                "recently": recently, 
                                "upcoming": upcoming, 
                                "last_week": last_week
                            }))
    response.set_cookie("subkey", subkey)
    return response

def email_task(request):
    subscriptions = Subscription.all().filter("activated_mail =", True)
    for s in subscriptions:
        s.add_email_task()
    return HttpResponse("Done: \n%s, %d" % (subscriptions, len(subscriptions)))

@is_post
def send_mail(request):
    key = None
    try:
        key = request.POST.get("key", None)
        if key is None:
            raise Http404
        subscription = Subscription.get(key)
        if subscription is None:
            raise Http404
        subscription.last_visited = datetime.datetime.now()
        subscription.put()
        context = subscription.get_message_context()
        if context is None:
            return HttpResponse("Nothing to do.")
        subject = "Seriesly.com - %d new episodes" % len(context["items"])
        body = render_to_string("subscription_mail.txt", RequestContext(request, context))
        mail.send_mail(settings.DEFAULT_FROM_EMAIL, subscription.email, subject, body)
    except Exception, e:
        logging.error(e)
        return HttpResponse("Done (with errors): %s" % key)
    logging.debug("Done sending Mail to %s" % subscription.email)
    return HttpResponse("Done: %s" % key)

        
def xmpp_task(request):
    subscriptions = Subscription.all().filter("activated_xmpp =", True)
    for s in subscriptions:
        s.add_xmpp_task()
    return HttpResponse("Done: \n%s, %d" % (subscriptions, len(subscriptions)))

@is_post
def send_xmpp(request):
    key = None
    try:
        key = request.POST.get("key", None)
        if key is None:
            raise Http404
        subscription = Subscription.get(key)
        if subscription is None:
            raise Http404
        subscription.last_visited = datetime.datetime.now()
        context = subscription.get_message_context()
        if context is None:
            return HttpResponse("Nothing to do.")
        body = render_to_string("subscription_xmpp.txt", RequestContext(request, context))
        status_code = xmpp.send_message(subscription.xmpp, body)
        chat_message_sent = (status_code != xmpp.NO_ERROR)
        if not chat_message_sent:
            subscription.xmpp_activated = False
        subscription.put()
    except Exception, e:
        logging.error(e)
        return HttpResponse("Done (with errors): %s" % key)
    logging.debug("Done sending XMPP to %s" % subscription.xmpp)
    return HttpResponse("Done: %s" % key)
    
@is_post
def edit_xmpp(request):
    form = XMPPSubscriptionForm(request.POST)
    if not form.is_valid():
        return show(request, request.POST.get("subkey", ""), extra_context={"xmpp_form":form})
    subscription = form._subscription
    if subscription.xmpp != form.cleaned_data["xmpp"]:
        subscription.activated_xmpp = False
    subscription.xmpp = form.cleaned_data["xmpp"]
    subscription.last_changed = datetime.datetime.now()
    subscription.put()
    if subscription.xmpp != "" and subscription.activated_xmpp == False:
        subscription.send_invitation_xmpp()
    return HttpResponseRedirect(subscription.get_absolute_url() + "#xmpp-subscription")
    
def incoming_xmpp(request):
    try:
        message = xmpp.Message(request.POST)
    except Exception, e:
        logging.warn("Failed to parse XMPP Message: %s" % e)
        return HttpResponse()
    sender = message.sender.split("/")[0]
    subscription = Subscription.all().filter("xmpp =", sender).get()
    if subscription is None:
        logging.warn("Sender not found: %s" % sender)
        return HttpResponse()
    if not subscription.activated_xmpp and message.body == "OK":
        subscription.activated_xmpp = True
        subscription.put()
        message.reply("Your Seriesly XMPP Subscription is now activated.")
    elif not subscription.activated_xmpp:
        message.reply("Someone requested this Seriesly Subscription to your XMPP address: %s . Please type 'OK' to confirm." % subscription.get_domain_absolute_url())
    else:
        message.reply("Your Seriesly XMPP Subscription is active. Go to %s to change settings."  % subscription.get_domain_absolute_url())
    return HttpResponse()

@is_post
def edit_webhook(request):
    form = WebHookSubscriptionForm(request.POST)
    if not form.is_valid():
        return show(request, request.POST.get("subkey", ""), extra_context={"webhook_form":form})
    subscription = form._subscription
    subscription.webhook = form.cleaned_data["webhook"]
    subscription.last_changed = datetime.datetime.now()
    subscription.put()
    return HttpResponseRedirect(subscription.get_absolute_url() + "#webhook-subscription")

def webhook_task(request):
    subscriptions = Subscription.all().filter("webhook !=", None)
    for s in subscriptions:
        s.add_webhook_task()
    return HttpResponse("Done: \n%s, %d" % (subscriptions, len(subscriptions)))

@is_post
def post_to_callback(request):
    key = None
    try:
        key = request.POST.get("key", None)
        if key is None:
            raise Http404
        subscription = Subscription.get(key)
        if subscription is None:
            raise Http404
        subscription.last_visited = datetime.datetime.now()
        context = subscription.get_message_context()
        if context is None:
            return HttpResponse("Nothing to do.")
        body = render_to_string("subscription_webhook.xml", RequestContext(request, context))
        try:
            subscription.post_to_callback(body)
        except Exception, e:
            subscription.webhook = None
            logging.warn("Webhook failed (%s): %s" % (key, e))
        subscription.put()
    except Exception, e:
        logging.error(e)
        return HttpResponse("Done (with errors): %s" % key)
    logging.debug("Done sending Webhook Callback to %s" % subscription.xmpp)
    return HttpResponse("Done: %s" % key)