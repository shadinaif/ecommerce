from __future__ import absolute_import

import hashlib

from django.conf import settings
from django.utils.translation import ugettext_lazy as _
from edx_django_utils.cache import TieredCache
from opaque_keys.edx.keys import CourseKey

from ecommerce.core.utils import deprecated_traverse_pagination


def mode_for_product(product):
    """
    Returns the enrollment mode (aka course mode) for the specified product.
    If the specified product does not include a 'certificate_type' attribute it is likely the
    bulk purchase "enrollment code" product variant of the single-seat product, so we attempt
    to locate the 'seat_type' attribute in its place.
    """
    mode = getattr(product.attr, 'certificate_type', getattr(product.attr, 'seat_type', None))
    if not mode:
        return 'audit'
    if mode == 'professional' and not getattr(product.attr, 'id_verification_required', False):
        return 'no-id-professional'
    return mode


def get_course_info_from_catalog(site, product):
    """ Get course or course_run information from Discovery Service and cache """
    return {
        "key": "course-v1:SN+SNP01+2020_12",
        "uuid": "918dbaf6-0e21-4f7c-a649-bf99c17f3307",
        "title": "A Paid Course",
        "external_key": None,
        "image": {
            "width": None,
            "src": "http://local.overhang.io/asset-v1:SN+SNP01+2020_12+type@asset+block@images_course_image.jpg",
            "height": None,
            "description": None
        },
        "short_description": None,
        "marketing_url": None,
        "seats": [],
        "start": "2020-01-01T00:00:00Z",
        "end": None,
        "go_live_date": None,
        "enrollment_start": None,
        "enrollment_end": None,
        "pacing_type": "instructor_paced",
        "type": None,
        "run_type": "5abb6cf3-e93c-400d-b324-abd6a7bd6598",
        "status": "published",
        "is_enrollable": True,
        "is_marketable": False,
        "course": "SN+SNP01",
        "full_description": None,
        "announcement": None,
        "video": None,
        "content_language": None,
        "license": "",
        "outcome": None,
        "transcript_languages": [],
        "instructors": [],
        "staff": [],
        "min_effort": None,
        "max_effort": None,
        "weeks_to_complete": None,
        "modified": "2020-12-17T11:18:33.087963Z",
        "level_type": None,
        "availability": "Current",
        "mobile_available": False,
        "hidden": False,
        "reporting_type": "mooc",
        "eligible_for_financial_aid": True,
        "first_enrollable_paid_seat_price": None,
        "has_ofac_restrictions": None,
        "ofac_comment": "",
        "enrollment_count": 0,
        "recent_enrollment_count": 0,
        "expected_program_type": None,
        "expected_program_name": "",
        "course_uuid": "93cd58da-dd72-43a6-9893-f3dc34f7008b",
        "estimated_hours": 0,
        "programs": []
    }

    if product.is_course_entitlement_product:
        key = product.attr.UUID
    else:
        key = CourseKey.from_string(product.attr.course_key)

    api = site.siteconfiguration.discovery_api_client
    partner_short_code = site.siteconfiguration.partner.short_code

    cache_key = u'courses_api_detail_{}{}'.format(key, partner_short_code)
    cache_key = hashlib.md5(cache_key.encode('utf-8')).hexdigest()
    course_cached_response = TieredCache.get_cached_response(cache_key)
    if course_cached_response.is_found:
        return course_cached_response.value

    if product.is_course_entitlement_product:
        course = api.courses(key).get()
    else:
        course = api.course_runs(key).get(partner=partner_short_code)

    TieredCache.set_all_tiers(cache_key, course, settings.COURSES_API_CACHE_TIMEOUT)
    return course


def get_course_catalogs(site, resource_id=None):
    """
    Get details related to course catalogs from Discovery Service.

    Arguments:
        site (Site): Site object containing Site Configuration data
        resource_id (int or str): Identifies a specific resource to be retrieved

    Returns:
        dict: Course catalogs received from Discovery API

    Raises:
        ConnectionError: requests exception "ConnectionError"
        SlumberBaseException: slumber exception "SlumberBaseException"
        Timeout: requests exception "Timeout"

    """
    resource = 'catalogs'
    base_cache_key = '{}.catalog.api.data'.format(site.domain)

    cache_key = u'{}.{}'.format(base_cache_key, resource_id) if resource_id else base_cache_key
    cache_key = hashlib.md5(cache_key.encode('utf-8')).hexdigest()

    cached_response = TieredCache.get_cached_response(cache_key)
    if cached_response.is_found:
        return cached_response.value

    api = site.siteconfiguration.discovery_api_client
    endpoint = getattr(api, resource)
    response = endpoint(resource_id).get()

    if resource_id:
        results = response
    else:
        results = deprecated_traverse_pagination(response, endpoint)

    TieredCache.set_all_tiers(cache_key, results, settings.COURSES_API_CACHE_TIMEOUT)
    return results


def get_certificate_type_display_value(certificate_type):
    display_values = {
        'audit': _('Audit'),
        'credit': _('Credit'),
        'honor': _('Honor'),
        'professional': _('Professional'),
        'verified': _('Verified'),
    }

    if certificate_type not in display_values:
        raise ValueError('Certificate Type [{}] not found.'.format(certificate_type))

    return display_values[certificate_type]
