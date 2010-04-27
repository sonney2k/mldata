"""
Define the views of app Repository

@var NUM_HISTORY_PAGE: how many versions of an item will be shown on history page
@type NUM_HISTORY_PAGE: integer
@var NUM_INDEX_PAGE: how many items will be shown on index/my page
@type NUM_INDEX_PAGE: integer
"""

import datetime, os, random, subprocess
from django.core import serializers
from django.core.paginator import Paginator, EmptyPage, InvalidPage
from django.core.servers.basehttp import FileWrapper
from django.core.files import File
from django.shortcuts import render_to_response, get_object_or_404
from django.http import HttpResponse, HttpResponseRedirect, HttpResponseForbidden, Http404
from django.db import IntegrityError, transaction
from django.utils.translation import ugettext as _
from django.forms.util import ErrorDict
from django.db.models import Q
from tagging.models import Tag, TaggedItem
from tagging.utils import calculate_cloud
from repository.models import *
from repository.forms import *
from settings import MEDIA_ROOT, TAG_SPLITSTR
from utils import h5conv


NUM_HISTORY_PAGE = 20
NUM_INDEX_PAGE = 10



def _get_object_or_404(klass, slug_or_id, version=None):
    """Wrapper for Django's get_object_or_404.

    Retrieves an item by slug or id and checks for ownership.

    @param slug_or_id: item's slug or id for lookup
    @type slug_or_id: string or integer
    @param klass: item's class for lookup in correct database table
    @type klass: either Data, Task or Solution
    @return: retrieved item
    @rtype: klass
    @raise Http404: if item could not be found
    """
    if version:
        obj = klass.objects.filter(
            slug__text=slug_or_id, is_deleted=False, version=version)
    else:
        obj = klass.objects.filter(
            slug__text=slug_or_id, is_deleted=False, is_current=True)

    if obj: # by slug
        obj = obj[0]
    else: # by id
        try:
            obj = klass.objects.get(pk=slug_or_id)
        except klass.DoesNotExist:
            raise Http404
        if not obj or obj.is_deleted:
            raise Http404

    return obj


def _get_versions_paginator(request, obj):
    """Get a paginator for item versions.

    @param request: request data
    @type request: Django request
    @param obj: item to get versions for
    @type obj: either class Data, Task or Solution
    @return: paginator for item versions
    @rtype: Django paginator
    """
    qs = Q(slug__text=obj.slug.text) & Q(is_deleted=False)
    items = obj.__class__.objects.filter(qs).order_by('version')
    items = [i for i in items if i.can_view(request.user)]
    paginator = Paginator(items, NUM_HISTORY_PAGE)

    try:
        # dunno a better way than looping thru, since index != obj.version
        index = 0
        for v in items:
            if v.id == obj.id:
                break
            else:
                index += 1
        default_page = (index / NUM_HISTORY_PAGE) + 1
        page = int(request.GET.get('page', str(default_page)))
    except ValueError:
        page = 1
    try:
        versions = paginator.page(page)
    except (EmptyPage, InvalidPage):
        versions = paginator.page(paginator.num_pages)

    return versions


def _get_completeness(obj):
    """Determine item's completeness.

    @param obj: item to determine its completeness from
    @type obj: either Data, Task or Solution
    @return: completeness of item as a percentage
    @rtype: integer
    @raise Http404: if given item is not of expected class
    """
    if obj.__class__ == Data:
        attrs = ['tags', 'description', 'license', 'summary', 'urls',
            'publications', 'source', 'measurement_details', 'usage_scenario']
    elif obj.__class__ == Task:
        attrs = ['tags', 'description', 'summary', 'urls', 'publications',
            'input', 'output', 'performance_measure', 'type', 'file']
    elif obj.__class__ == Solution:
        attrs = ['tags', 'description', 'summary', 'urls', 'publications',
            'feature_processing', 'parameters', 'os', 'code',
            'software_packages', 'score']
    else:
        raise Http404

    attrs_len = len(attrs)
    attrs_complete = 0
    for attr in attrs:
        if eval('obj.' + attr):
            attrs_complete += 1
    return int((attrs_complete * 100) / attrs_len)


def _get_rating_form(request, obj):
    """Get a rating form for given item.

    @param request: request data
    @type request: Django request
    @param obj: item to get rating form for
    @type obj: either of Data, Task, Solution
    @return: a rating form
    @rtype: forms.RatingForm
    """
    if not request.user.is_authenticated():
        return None

    current = obj.__class__.objects.get(slug=obj.slug, is_current=True)
    if not current:
        return None

    klassname = current.__class__.__name__
    rklass = eval(klassname + 'Rating')
    try:
        r = rklass.objects.get(user=request.user, repository=current)
        rating_form = RatingForm({'interest': r.interest, 'doc': r.doc})
    except rklass.DoesNotExist:
        rating_form = RatingForm()
    rating_form.action = reverse(
        eval(klassname.lower() + '_rate'), args=[current.id])

    return rating_form


def _get_latest(request):
    """Get latest items of each type.

    @param request: request data
    @type request: Django request
    @return: latest items of each type
    @rtype: dict
    """
    qs = Q(is_deleted=False) & (Q(is_public=True) | Q(user=request.user))
    latest = {}
    try:
        latest['data'] = Data.objects.filter(qs).order_by('-pub_date')[0]
    except IndexError:
        latest['data'] = None
    try:
        latest['task'] = Task.objects.filter(qs).order_by('-pub_date')[0]
    except IndexError:
        latest['task'] = None
    try:
        latest['solution'] = Solution.objects.filter(qs).order_by('-pub_date')[0]
    except IndexError:
        latest['solution'] = None

    return latest



def _get_current_tagged_items(request, klass, tagged):
    """Get current items with specific tag.

    @param klass: klass to find current items for
    @type klass: one of Data, Task, Solution
    @param tagged: tagged items
    @type tagged: list of TaggedItem
    @return: current tagged items
    @rtype: list of Data, Task, Solution
    """
    # without if-construct sqlite3 barfs on AnonymousUser
    if request.user.id:
        qs = (Q(user=request.user) | Q(is_public=True)) & Q(is_current=True)
    else:
        qs = Q(is_public=True) & Q(is_current=True)

    current = klass.objects.filter(qs).order_by('name')
    l = []
    for c in current:
        for t in tagged:
            if t.object_id == c.id:
                l.append(c)
                break
    return l



def _get_current_tags(request):
    """Get current tags available to user.

    # FIXME: looks inefficient

    @param request: request data
    @type request: Django request
    @return: current tags available to user
    @rtype: list of tagging.Tag
    """
    # without if-construct sqlite3 barfs on AnonymousUser
    if request.user.id:
        qs = (Q(user=request.user) | Q(is_public=True)) & Q(is_current=True)
    else:
        qs = Q(is_public=True) & Q(is_current=True)

    current = list(Data.objects.filter(qs))
    current.extend(list(Task.objects.filter(qs)))
    current.extend(list(Solution.objects.filter(qs)))
    if current:
        all = Tag.objects.all()
        tags = []
        for tag in all:
            found = False
            tag.count = 0
            for item in tag.items.values():
                for c in current:
                    if item['object_id'] == c.id:
                        found = True
                        tag.count += 1
            if found:
                tags.append(tag)
    else:
        tags = None

    return tags



def _get_tag_cloud(request):
    """Retrieve a cloud of tags of all item types.

    @param request: request data
    @type request: Django request
    @return: list of tags with attributes font_size
    @rtype: list of tagging.Tag
    """
    current = _get_current_tags(request)
    if current:
        cloud = calculate_cloud(current, steps=2)
        random.shuffle(cloud)
    else:
        cloud = None
    return cloud



@transaction.commit_on_success
def _activate(request, klass, id):
    """Activate item given by id and klass.

    @param request: request data
    @type request: Django request
    @param klass: item's class for lookup in correct database table
    @type klass: either Data, Task or Solution
    @param id: id of the item to activate
    @type id: integer
    @return: redirect user to login page or item's page
    @rtype: Django response
    """
    if not request.user.is_authenticated():
        func = eval(klass.__name__.lower() + '_activate')
        url = reverse(func, args=[id])
        return HttpResponseRedirect(reverse('user_signin') + '?next=' + url)

    obj = _get_object_or_404(klass, id)
    if obj.can_activate(request.user):
        obj.is_public = True
        obj.set_current()

    return HttpResponseRedirect(obj.get_absolute_slugurl())


@transaction.commit_on_success
def _delete(request, klass, id):
    """Delete item given by id and klass.

    @param request: request data
    @type request: Django request
    @param klass: item's class for lookup in correct database table
    @type klass: either Data, Task or Solution
    @param id: id of the item to delete
    @type id: integer
    @return: redirect user to login page or item's page or user's my page
    @rtype: Django response
    """
    if not request.user.is_authenticated():
        func = eval(klass.__name__.lower() + '_delete')
        url = reverse(func, args=[id])
        return HttpResponseRedirect(reverse('user_signin') + '?next=' + url)

    obj = _get_object_or_404(klass, id)
    if not obj.can_delete(request.user):
        return HttpResponseForbidden()
    obj.is_deleted = True
    obj.save()

    # set new current
    obj = klass.objects.filter(slug=obj.slug).\
        filter(is_deleted=False).order_by('-version')
    if obj:
        obj[0].set_current()
        return HttpResponseRedirect(obj[0].get_absolute_slugurl())

    func = eval(klass.__name__.lower() + '_my')
    return HttpResponseRedirect(reverse(func))


def _download_hit(obj):
    """Increase hit counter for given object.

    @param obj: object to increase counter for
    @type obj: Repository
    """
    current = obj.__class__.objects.get(slug=obj.slug, is_current=True)
    current.downloads += 1
    current.save()



def _download(request, klass, id):
    """Download file relating to item given by id and klass.

    @param request: request data
    @type request: Django request
    @param klass: item's class for lookup in correct database table
    @type klass: either Data, Task or Solution
    @param id: id of the relating item
    @type id: integer
    @return: contents of file related to object
    @rtype: binary file
    @raise Http404: if given klass is unexpected or file doesn't exist
    """
    obj = _get_object_or_404(klass, id)
    if not obj.can_download(request.user):
        return HttpResponseForbidden()

    if klass == Data or klass == Task:
        fileobj = obj.file
    elif klass == Solution:
        fileobj = obj.score
    else:
        raise Http404

    if not fileobj: # maybe no file attached to this item
        raise Http404

    # fails to work when OpenID Middleware is activated
#    filename = os.path.join(MEDIA_ROOT, fileobj.name)
#    wrapper = FileWrapper(file(filename))
#    response = HttpResponse(wrapper, content_type='application/octet-stream')
    # not sure if this alternative is a memory hog...
    response = HttpResponse()
    response['Content-Type'] = 'application/octet-stream'
    try:
        response['Content-Length'] = fileobj.size
        response['Content-Disposition'] = 'attachment; filename=' +\
            fileobj.name.split(os.sep)[-1]
        for chunk in fileobj.chunks():
            response.write(chunk)
    except OSError, err: # something wrong with file, maybe not existing
        raise OSError(_('Missing file') + ': ' + fileobj.name)


    _download_hit(obj)
    return response



def data_download_xml(request, id):
    """Download XML file relating to item given by id.

    @param request: request data
    @type request: Django request
    @param id: id of the relating item
    @type id: integer
    @return: contents of file related to object
    @rtype: binary file
    @raise Http404: file doesn't exist
    """
    obj = _get_object_or_404(Data, id)
    if not obj.can_download(request.user):
        return HttpResponseForbidden()

    if not obj.file: # maybe no file attached to this item
        raise Http404


    try:
        fname = os.path.join(MEDIA_ROOT, obj.file.name + '.xml')
        if not os.path.exists(fname):
            cmd = 'h5dump --xml ' + os.path.join(MEDIA_ROOT, obj.file.name) + ' > ' + fname
            if not subprocess.call(cmd, shell=True) == 0:
                raise IOError('Unsuccessful conversion to XML: ' + cmd)
        fileobj = File(open(fname, 'r'))

        response = HttpResponse()
        response['Content-Type'] = 'application/xml'
        response['Content-Length'] = fileobj.size
        response['Content-Disposition'] = 'attachment; filename=' +\
            fileobj.name.split(os.sep)[-1]
        for chunk in fileobj.chunks():
            response.write(chunk)
    except OSError, err: # something wrong with file, maybe not existing
        raise OSError(_('Missing file') + ': ' + fileobj.name)


    _download_hit(obj)
    return response



def _view(request, klass, slug_or_id, version=None):
    """View item given by slug and klass.

    @param request: request data
    @type request: Django request
    @param klass: item's class for lookup in correct database table
    @type klass: either Data, Task or Solution
    @param slug_or_id: slug or id of the item to view
    @type slug_or_id: string or integer
    @return: view page or review page if klass Data and item not approved
    @rtype: Django response
    """
    obj = _get_object_or_404(klass, slug_or_id, version)
    if not obj.can_view(request.user):
        return HttpResponseForbidden()
    if klass == Data and not obj.is_approved:
        return HttpResponseRedirect(reverse(data_new_review, args=[obj.slug]))

    current = obj.__class__.objects.get(slug=obj.slug, is_current=True)
    current.hits += 1
    current.save()

    obj.completeness = _get_completeness(obj)
    obj.klass = klass.__name__
    # need tags in list
    obj.tags = obj.tags.split(TAG_SPLITSTR)
    obj.versions = _get_versions_paginator(request, obj)
    klassname = klass.__name__.lower()
    obj.url_activate = reverse(eval(klassname + '_activate'), args=[obj.id])
    obj.url_edit = reverse(eval(klassname + '_edit'), args=[obj.id])
    obj.url_delete = reverse(eval(klassname + '_delete'), args=[obj.id])

    # breadcrumbs
    if klass == Solution:
        obj.d = obj.task.data
    elif klass == Task:
        obj.d = obj.data

    info_dict = {
        'object': obj,
        'request': request,
        'can_activate': obj.can_activate(request.user),
        'can_delete': obj.can_delete(request.user),
        'current': current,
        'rating_form': _get_rating_form(request, obj),
        'tagcloud': _get_tag_cloud(request),
        'section': 'repository',
    }
    if hasattr(obj, 'data_heldback') and obj.data_heldback:
        info_dict['can_view_heldback'] = obj.data_heldback.can_view(request.user)

    if klass == Data:
        from h5py import h5e
        try:
            h = h5conv.HDF5()
            info_dict['extract'] = h.get_extract(
                os.path.join(MEDIA_ROOT, obj.file.name))
        except IOError, err:
            raise IOError(err.message + ': ' + obj.file.name)
        except h5e.LowLevelIOError: # ignore if not HDF5 file
            info_dict['extract'] = None

    return render_to_response('repository/item_view.html', info_dict)



@transaction.commit_on_success
def _new(request, klass):
    """Create a new item of given klass.

    @param request: request data
    @type request: Django request
    @param klass: item's class for lookup in correct database table
    @type klass: either Data, Task or Solution
    @return: user login page, item's view page or this page again on failed form validation
    @rtype: Django response
    @raise Http404: if given klass is unexpected
    """
    url_new = reverse(eval(klass.__name__.lower() + '_new'))
    if not request.user.is_authenticated():
        return HttpResponseRedirect(reverse('user_signin') + '?next=' + url_new)

    formfunc = eval(klass.__name__ + 'Form')
    if request.method == 'POST':
        form = formfunc(request.POST, request.FILES, request=request)

        # manual validation coz it's required for new, but not edited Data
        if not request.FILES and klass == Data:
            form.errors['file'] = ErrorDict({'': _('This field is required.')}).as_ul()

        if form.is_valid():
            new = form.save(commit=False)
            new.pub_date = datetime.datetime.now()
            try:
                new.slug = new.make_slug()
            except IntegrityError:
                # looks quirky...
                d = ErrorDict({'':
                    _('The given name yields an already existing slug. Please try another name.')})
                form.errors['name'] = d.as_ul()
            else:
                new.version = 1
                new.is_current = True
                new.is_public = False
                new.user = request.user

                if not form.cleaned_data['keep_private']:
                    new.is_public = True

                if klass == Data:
                    new.file = request.FILES['file']
                    h = h5conv.HDF5()
                    new.format = h.get_fileformat(request.FILES['file'].name)
                    new.file.name = new.get_filename()
                    new.num_instances = new.num_attributes = 0
                    new.save()
                elif klass == Task:
                    if 'file' in request.FILES:
                        new.file = request.FILES['file']
                        new.file.name = new.get_filename()
                    new.license = FixedLicense.objects.get(pk=1) # fixed to CC-BY-SA
                    new.save()
                elif klass == Solution:
                    if 'score' in request.FILES:
                        new.score = request.FILES['score']
                        new.score.name = new.get_scorename()
                    new.license = FixedLicense.objects.get(pk=1) # fixed to CC-BY-SA
                    new.save()
                else:
                    raise Http404
                return HttpResponseRedirect(new.get_absolute_slugurl())
    else:
        form = formfunc(request=request)

    info_dict = {
        'klass': klass.__name__,
        'url_new': url_new,
        'form': form,
        'request': request,
        'tagcloud': _get_tag_cloud(request),
        'section': 'repository',
    }

    return render_to_response('repository/item_new.html', info_dict)


@transaction.commit_on_success
def _edit(request, klass, id):
    """Edit existing item given by slug or id and klass.

    @param request: request data
    @type request: Django request
    @param klass: item's class for lookup in correct database table
    @type klass: either Data, Task or Solution
    @param id: id of the item to activate
    @type id: integer
    @return: user login page, item's view page or this page again on failed form validation
    @rtype: Django response
    @raise Http404: if given klass is unexpected
    """
    prev = _get_object_or_404(klass, id)
    prev.klass = klass.__name__
    prev.url_edit = reverse(
        eval(klass.__name__.lower() + '_edit'), args=[prev.id])
    if not request.user.is_authenticated():
        return HttpResponseRedirect(reverse('user_signin') + '?next=' + prev.url_edit)
    if not prev.can_edit(request.user):
        return HttpResponseForbidden()

    formfunc = eval(klass.__name__ + 'Form')
    if request.method == 'POST':
        request.POST['name'] = prev.name # cheat a little
        form = formfunc(request.POST, request=request)

        if form.is_valid():
            next = form.save(commit=False)
            next.pub_date = datetime.datetime.now()
            next.slug = prev.slug
            next.version = next.get_next_version()
            next.user = request.user

            if prev.is_public: # once public, always public
                next.is_public = True
            elif not form.cleaned_data['keep_private']:
                next.is_public = True

            if klass == Data:
                next.format = prev.format
                next.is_approved = prev.is_approved
                next.file = prev.file
                next.save()
            elif klass == Task:
                if 'file' in request.FILES:
                    next.file = request.FILES['file']
                    next.file.name = next.get_filename()
                    filename = os.path.join(MEDIA_ROOT, prev.file.name)
                    if os.path.isfile(filename):
                        os.remove(filename)
                else:
                    next.file = prev.file

                next.license = FixedLicense.objects.get(pk=1) # fixed to CC-BY-SA
                next.save()
            elif klass == Solution:
                if 'score' in request.FILES:
                    next.score = request.FILES['score']
                    next.score.name = next.get_scorename()
                    filename = os.path.join(MEDIA_ROOT, prev.score.name)
                    if os.path.isfile(filename):
                        os.remove(filename)
                else:
                    next.score = prev.score
                next.license = FixedLicense.objects.get(pk=1) # fixed to CC-BY-SA
                next.save()
            else:
                raise Http404

            form.save_m2m() # for publications
            next.set_current()
            return HttpResponseRedirect(next.get_absolute_slugurl())
    else:
        form = formfunc(instance=prev, request=request)

    info_dict = {
        'form': form,
        'object': prev,
        'request': request,
        'publication_form': PublicationForm(),
        'tagcloud': _get_tag_cloud(request),
        'section': 'repository',
    }

    return render_to_response('repository/item_edit.html', info_dict)



def _index(request, klass, my=False):
    """Index/My page for section given by klass.

    @param request: request data
    @type request: Django request
    @param klass: item's class for lookup in correct database table
    @type klass: either Data, Task or Solution
    @param my: if the page should be a My page or the archive index of the section
    @return: section's index or My page
    @rtype: Django response
    """
    objects = klass.objects.filter(is_deleted=False, is_current=True).order_by('-pub_date')
    if my and request.user.is_authenticated():
        objects = objects.filter(user=request.user)
        if klass == Data:
            unapproved = klass.objects.filter(
                user=request.user, is_approved=False
            )
        else:
            unapproved = None
        my_or_archive = _('My')
    else:
        objects = objects.filter(is_public=True)
        unapproved = None
        my_or_archive = _('Public Archive')

    paginator = Paginator(objects, NUM_INDEX_PAGE)
    try:
        num = int(request.GET.get('page', '1'))
    except ValueError:
        num = 1
    try:
        page = paginator.page(num)
    except (EmptyPage, InvalidPage):
        page = paginator.page(paginator.num_pages)

    info_dict = {
        'request': request,
        'page': page,
        'klass': klass.__name__,
        'unapproved': unapproved,
        'my_or_archive': my_or_archive,
        'tagcloud': _get_tag_cloud(request),
        'section': 'repository',
    }
    return render_to_response('repository/item_index.html', info_dict)



def index(request):
    """Index page of app repository.

    @param request: request data
    @type request: Django request
    @return: rendered response page
    @rtype: Django response
    """
    info_dict = {
#        'latest': _get_latest(request),
        'request': request,
        'section': 'repository',
        'tagcloud': _get_tag_cloud(request),
    }
    return render_to_response('repository/index.html', info_dict)



def data_index(request):
    """Index page of Data section.

    @param request: request data
    @type request: Django request
    @return: rendered response page
    @rtype: Django response
    """
    return _index(request, Data)

def data_my(request):
    """My page of Data section.

    @param request: request data
    @type request: Django request
    @return: rendered response page
    @rtype: Django response
    """
    return _index(request, Data, True)

def data_new(request):
    """New page of Data section.

    @param request: request data
    @type request: Django request
    @return: rendered response page
    @rtype: Django response
    """
    return _new(request, Data)

def data_view(request, id):
    """View Data item by id.

    @param request: request data
    @type request: Django request
    @param id: id of item to view
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _view(request, Data, id)


def data_view_slug(request, slug, version=None):
    """View Data item by slug.

    @param request: request data
    @type request: Django request
    @param slug: slug of item to view
    @type slug: string
    @param version: version of item to view
    @type version: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _view(request, Data, slug, version)

def data_edit(request, id):
    """Edit page of Data section.

    @param request: request data
    @type request: Django request
    @param id: id of item to edit
    @type id: string or integer
    @return: rendered response page
    @rtype: Django response
    """
    return _edit(request, Data, id)

def data_delete(request, id):
    """Delete of Data section.

    @param request: request data
    @type request: Django request
    @param id: id of item to delete
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _delete(request, Data, id)

def data_activate(request, id):
    """Activate of Data section.

    @param request: request data
    @type request: Django request
    @param id: id of item to activate
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _activate(request, Data, id)

def data_download(request, id):
    """Download of Data section.

    @param request: request data
    @type request: Django request
    @param id: id of item to download
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _download(request, Data, id)


def task_index(request):
    """Index page of Task section.

    @param request: request data
    @type request: Django request
    @return: rendered response page
    @rtype: Django response
    """
    return _index(request, Task)

def task_my(request):
    """My page of Task section.

    @param request: request data
    @type request: Django request
    @return: rendered response page
    @rtype: Django response
    """
    return _index(request, Task, True)

def task_new(request):
    """New page of Task section.

    @param request: request data
    @type request: Django request
    @return: rendered response page
    @rtype: Django response
    """
    return _new(request, Task)

def task_view(request, id):
    """View Task item by id.

    @param request: request data
    @type request: Django request
    @param id: id of item to view
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _view(request, Task, id)

def task_view_slug(request, slug_data, slug_task, version=None):
    """View Task item by slug.

    @param request: request data
    @type request: Django request
    @param slug_data: data slug  of the item to view
    @type slug_data: string
    @param slug_task: task slug  of the item to view
    @type slug_task: string
    @param version: version of item to view
    @type version: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _view(request, Task, slug_task, version)

def task_edit(request, id):
    """Edit page of Task section.

    @param request: request data
    @type request: Django request
    @param id: id of item to edit
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _edit(request, Task, id)

def task_delete(request, id):
    """Delete of Task section.

    @param request: request data
    @type request: Django request
    @param id: id of the item to delete
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _delete(request, Task, id)

def task_activate(request, id):
    """Activate of Task section.

    @param request: request data
    @type request: Django request
    @param id: id of the item to activate
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _activate(request, Task, id)

def task_download(request, id):
    """Download of Task section.

    @param request: request data
    @type request: Django request
    @param id: id of the item to download
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _download(request, Task, id)


def solution_index(request):
    """Index page of Solution section.

    @param request: request data
    @type request: Django request
    @return: rendered response page
    @rtype: Django response
    """
    return _index(request, Solution)

def solution_my(request):
    """My page of Solution section.

    @param request: request data
    @type request: Django request
    @return: rendered response page
    @rtype: Django response
    """
    return _index(request, Solution, True)

def solution_new(request):
    """New page of Solution section.

    @param request: request data
    @type request: Django request
    @return: rendered response page
    @rtype: Django response
    """
    return _new(request, Solution)

def solution_view(request, id):
    """View Solution item by id.

    @param request: request data
    @type request: Django request
    @param id: id of item to view
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _view(request, Solution, id)

def solution_view_slug(request, slug_data, slug_task, slug_solution, version=None):
    """View page of Solution section.

    @param request: request data
    @type request: Django request
    @param slug_data: data slug of the item to view
    @type slug_data: string
    @param slug_task: task slug of the item to view
    @type slug_task: string
    @param slug_solution: solution slug of the item to view
    @type slug_solution: string
    @param version: version of item to view
    @type version: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _view(request, Solution, slug_solution, version)

def solution_edit(request, id):
    """Edit page of Solution section.

    @param request: request data
    @type request: Django request
    @param id: id of item to edit
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _edit(request, Solution, id)

def solution_activate(request, id):
    """Activate of Solution section.

    @param request: request data
    @type request: Django request
    @param id: id of the item to activate
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _activate(request, Solution, id)

def solution_delete(request, id):
    """Delete of Solution section.

    @param request: request data
    @type request: Django request
    @param id: id of the item to delete
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _delete(request, Solution, id)

def score_download(request, id):
    """Download of Solution section.

    @param request: request data
    @type request: Django request
    @param id: id of the item to download
    @type id: integer
    @return: rendered response page
    @rtype: Django response
    """
    return _download(request, Solution, id)



@transaction.commit_on_success
def data_new_review(request, slug):
    """Review Data item to check if uploaded file is as expected.

    @param request: request data
    @type request: Django request
    @param slug: slug of the item to review
    @type slug: string
    @return: redirect user to login page or item's view page after approval or review form
    @rtype: Django response
    """
    if not request.user.is_authenticated():
        next = '?next=' + reverse(data_new_review, args=[slug])
        return HttpResponseRedirect(reverse('user_signin') + next)

    obj = _get_object_or_404(Data, slug)
    # don't want users to be able to remove items once approved
    if not obj.can_edit(request.user) or obj.is_approved:
        return HttpResponseForbidden()

    hdf5 = h5conv.HDF5()
    if request.method == 'POST':
        if request.POST.has_key('revert'):
            os.remove(os.path.join(MEDIA_ROOT, obj.file.name))
            obj.delete()
            return HttpResponseRedirect(reverse(data_new))
        elif request.POST.has_key('approve'):
            uploaded = os.path.join(MEDIA_ROOT, obj.file.name)
            converted = hdf5.get_filename(uploaded)

            format = request.POST['id_format'].lower()
            if format != 'h5':
                try:
                    hdf5.convert(uploaded, format, converted, 'h5')
                    format = 'h5'
                except Exception:
                    pass
            obj.format = format
            (obj.num_instances, obj.num_attributes) =\
                hdf5.get_num_instattr(converted)

            if os.path.isfile(converted): # assign converted file to obj
                os.remove(uploaded)
                # for some reason, FileField saves file.name as DATAPATH/<basename>
                obj.file.name = os.path.sep.join([DATAPATH, converted.split(os.path.sep)[-1]])

            obj.is_approved = True
            obj.save()
            return HttpResponseRedirect(reverse(data_view_slug, args=[obj.slug]))

    info_dict = {
        'object': obj,
        'request': request,
        'tagcloud': _get_tag_cloud(request),
        'section': 'repository',
        'extract': hdf5.get_extract(os.path.join(MEDIA_ROOT, obj.file.name)),
    }
    return render_to_response('repository/data_new_review.html', info_dict)



def tags_index(request):
    """Index page to display all public and all user's tags.

    @param request: request data
    @type request: Django request
    @return: rendered response page
    @rtype: Django response
    """
    info_dict = {
        'request': request,
        'section': 'repository',
        'tags': _get_current_tags(request),
    }
    return render_to_response('repository/tags_index.html', info_dict)


def tags_view(request, tag):
    """View all items tagged by given tag.

    @param request: request data
    @type request: Django request
    @param tag: name of the tag
    @type tag: string
    @return: rendered response page
    @rtype: Django response
    @raise Http404: if an item's class is unexpected
    """
    try:
        tag = Tag.objects.get(name=tag)
        tagged = TaggedItem.objects.filter(tag=tag)
        objects = {
            'data': _get_current_tagged_items(request, Data, tagged),
            'task': _get_current_tagged_items(request, Task, tagged),
            'solution': _get_current_tagged_items(request, Solution, tagged),
        }
    except Tag.DoesNotExist:
        objects = None

    info_dict = {
        'request': request,
        'section': 'repository',
        'tag': tag,
        'tagcloud': _get_tag_cloud(request),
        'objects': objects,
    }
    return render_to_response('repository/tags_view.html', info_dict)



def _rate(request, klass, id):
    """Rate an item given by id and klass.

    @param request: request data
    @type request: Django request
    @param klass: item's class for lookup in correct database table
    @type klass: either Data, Task or Solution
    @param id: item's id
    @type id: integer
    @return: redirect to item's view page
    @rtype: Django response
    """
    if not request.user.is_authenticated():
        next = '?next=' + reverse(eval(klass.__name__.lower() + '_rate'), args=[id])
        return HttpResponseRedirect(reverse('user_signin') + next)

    rklass = eval(klass.__name__ + 'Rating')
    obj = get_object_or_404(klass, pk=id)
    if request.method == 'POST':
        form = RatingForm(request.POST)
        if form.is_valid():
            r, fail = rklass.objects.get_or_create(user=request.user, repository=obj)
            r.update(form.cleaned_data['interest'], form.cleaned_data['doc'])

    return HttpResponseRedirect(obj.get_absolute_slugurl())


def data_rate(request, id):
    return _rate(request, Data, id)
def task_rate(request, id):
    return _rate(request, Task, id)
def solution_rate(request, id):
    return _rate(request, Solution, id)



@transaction.commit_on_success
def publication_edit(request):
    """Edit/New page of a publication.

    @param request: request data
    @type request: Django request
    @return: rendered response page
    @rtype: Django response
    """
    if not request.user.is_authenticated():
        if request.method == 'POST':
            next = '?next=' + request.POST['next']
        else:
            next = ''
        return HttpResponseRedirect(reverse('user_signin') + next)

    if request.method == 'POST':
        # work around a peculiarity within django
        pub = None
        id = int(request.POST['id'])
        if id > 0:
            try:
                pub = Publication.objects.get(pk=id)
                form = PublicationForm(request.POST, instance=pub)
            except Publication.DoesNotExist:
                form = PublicationForm(request.POST)
        else:
            form = PublicationForm(request.POST)

        if form.is_valid():
            if not pub:
                pub = Publication()
            pub.content = form.cleaned_data['content']
            pub.title = form.cleaned_data['title']
            pub.save()
            return HttpResponseRedirect(form.cleaned_data['next'])
        else:
            print request.POST
            print form.errors
            return HttpResponseRedirect(form.cleaned_data['next'])
    return HttpResponseRedirect(reverse('repository_index'))


def publication_get(request, id):
    """AJAX: Get publication specified by id.

    @param request: request data
    @type request: Django request
    @param id: id of item to edit
    @type id: integer
    @return: publication content in response page
    @rtype: Django response
    """
    try:
        data = serializers.serialize('json',
            [Publication.objects.get(pk=id)], fields=('title', 'content'))
    except Publication.DoesNotExist:
        data = '[{"pk": 0, "model": "repository.publication", "fields": {"content": "", "title": ""}}]'

    return HttpResponse(data, mimetype='text/plain')
