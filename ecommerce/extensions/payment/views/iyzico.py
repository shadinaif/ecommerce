""" Views for interacting with the payment processor. """
from __future__ import absolute_import, unicode_literals

import logging
import os

import waffle
from django.conf import settings
from django.core.exceptions import MultipleObjectsReturned
from django.core.management import call_command
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.utils.six import StringIO
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View, TemplateView
from edx_rest_api_client.client import EdxRestApiClient
from edx_rest_api_client.exceptions import SlumberHttpBaseException
from oscar.apps.partner import strategy
from oscar.apps.payment.exceptions import PaymentError
from oscar.core.loading import get_class, get_model
from requests.exceptions import Timeout

from ecommerce.core.url_utils import get_lms_url
from ecommerce.extensions.analytics.utils import parse_tracking_context
from ecommerce.extensions.basket.utils import basket_add_organization_attribute
from ecommerce.extensions.checkout.mixins import EdxOrderPlacementMixin
from ecommerce.extensions.checkout.utils import get_receipt_page_url
from ecommerce.extensions.offer.constants import DYNAMIC_DISCOUNT_FLAG
from ecommerce.extensions.payment.processors.iyzico import Iyzico

import iyzipay
import json
import inspect

logger = logging.getLogger(__name__)

Applicator = get_class('offer.applicator', 'Applicator')
Basket = get_model('basket', 'Basket')
BillingAddress = get_model('order', 'BillingAddress')
Country = get_model('address', 'Country')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')
CartLine = get_model('basket', 'Line')


def get_basket_id_from_iyzico_id(iyzico_id):
    dot_placement = iyzico_id.rfind('.')
    logger.info('---------------dot_placement = {}'.format(dot_placement))
    logger.info('---------------iyzico_id[dot_placement:] = {}'.format(iyzico_id[dot_placement:]))
    return int(iyzico_id[dot_placement+1:] if dot_placement > 0 else 0)


def get_iyzico_id_from_basket_id(basket_id):
    return 'ozogretmen.courses.basket.{basket_id}'.format(basket_id=basket_id)


class IyzicoInitializationException(ValueError):
    pass


class IyzicoPaymentView(View):
    iyzico_template_name = 'payment/iyzico.html'
    error_template_name = 'payment/iyzico_callback_failed.html'

    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):  # pylint: disable=arguments-differ
        """
        Request needs to be csrf_exempt to handle POST back from external payment processor.
        """
        logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
        return super(IyzicoPaymentView, self).dispatch(*args, **kwargs)

    @property
    def payment_processor(self):
        logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
        return Iyzico(self.request.site)

    @staticmethod
    def _get_address(user):
        address = dict([('address', '--')])
        address['contactName'] = user.username
        address['city'] = '--'
        address['country'] = '--'

        return address

    @staticmethod
    def _get_buyer_info(user):
        _, _, ip = parse_tracking_context(user, usage='embargo')
        buyer = dict([('id', str(user.id))])
        buyer['name'] = user.username
        buyer['surname'] = '--'
        buyer['email'] = user.email
        buyer['identityNumber'] = '-----'
        buyer['registrationAddress'] = '--'
        buyer['ip'] = ip or 'unknown'
        buyer['city'] = '--'
        buyer['country'] = '--'

        return buyer

    def initialize_form(self, basket, lang):
        logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
        options = {
            'api_key': self.payment_processor.api_key,
            'secret_key': self.payment_processor.secret_key,
            'base_url': self.payment_processor.base_url,
        }
        items = CartLine.objects.filter(basket=basket)
        logger.info('---------------items.count = {}'.format(items.count()))

        if items.count() == 0:
            raise CartLine.DoesNotExist

        total_price = 0
        request = dict([('locale', lang)])
        request['basketId'] = get_iyzico_id_from_basket_id(basket_id=basket.id)
        request['callbackUrl'] = 'http://ecommerce.local.overhang.io/payment/iyzico/execute/'

        request['buyer'] = self._get_buyer_info(user=basket.owner)

        address = self._get_address(user=basket.owner)
        request['shippingAddress'] = address
        request['billingAddress'] = address

        basket_items = []
        for item in items:
            logger.info('---------------item.product.course.id = {}'.format(item.product.course.id))
            basket_item = dict([('id', str(item.id))])
            basket_item['name'] = str(item.product.course.id)
            basket_item['category1'] = 'Courses'
            basket_item['itemType'] = 'VIRTUAL'
            basket_item['price'] = str(item.price_incl_tax)
            basket_items.append(basket_item)

            total_price += float(basket_item['price'])

        request['basketItems'] = basket_items

        request['price'] = str(total_price)
        request['paidPrice'] = str(total_price)

        init = iyzipay.CheckoutFormInitialize()
        logger.info('---------------options = {}'.format(options))
        return init.create(request, options)

    def _update_context(self, request, context, basket):
        logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
        context['error'] = ''
        response = None
        try:
            lang = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
            logger.info('---------------lang = {}'.format(lang))
            response = self.initialize_form(basket=basket, lang=lang)
        except CartLine.DoesNotExist:
            raise CartLine.DoesNotExist
        except Exception as e:
            logger.exception('Iyzico form initialization failed!')
            logger.exception(str(e))

        if response is None:
            context['error'] = 'Something went wrong. Please try again later.'
            logger.exception('Empty response from Iyzico!')
        else:
            bytes_data = response.read()
            data = json.loads(bytes_data)
            logger.info('---------------data = {}'.format(data))
            if data['status'] != 'success':
                raise IyzicoInitializationException(
                    'Iyzico Initialization Error {}: {}'.format(data['errorCode'], data['errorMessage'])
                )
            context['iyzico'] = data['checkoutFormContent']

    def post(self, request, basket_id):
        logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
        user = request.user
        logger.info('---------------Basket Count = {}'.format(Basket.objects.filter().count()))
        logger.info('---------------basket_id = {}'.format(basket_id))
        context = {}
        try:
            basket = Basket.objects.get(pk=basket_id)
        except Basket.DoesNotExist:
            template_name = self.error_template_name
        else:
            if basket.owner == user:
                try:
                    self._update_context(request, context, basket)
                except CartLine.DoesNotExist:
                    template_name = self.error_template_name
                except IyzicoInitializationException:
                    template_name = self.error_template_name
                else:
                    template_name = self.iyzico_template_name
            else:
                template_name = self.error_template_name

        return render(request, template_name, context)


class IyzicoPaymentExecutionView(EdxOrderPlacementMixin, View):
    @property
    def payment_processor(self):
        logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
        return Iyzico(self.request.site)

    @method_decorator(transaction.non_atomic_requests)
    @method_decorator(csrf_exempt)
    def dispatch(self, *args, **kwargs):  # pylint: disable=arguments-differ
        """
        Request needs to be csrf_exempt to handle POST back from external payment processor.
        """
        logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
        return super(IyzicoPaymentExecutionView, self).dispatch(*args, **kwargs)

    @staticmethod
    def _get_basket(basket_id):
        Basket.objects.get()

    def post(self, request):
        logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
        iyzico_response = request.POST.dict()
        token = iyzico_response.get('token', 'nothing')
        logger.info('---------------token = {}'.format(token))
        data = self.payment_processor.retrieve_payment_info(token=token)
        logger.info('---------------data = {}'.format(data))

        if data is None:
            return redirect(self.payment_processor.error_url)

        basket_id = get_basket_id_from_iyzico_id(data['basketId'])
        logger.info('---------------basket_id = {}'.format(basket_id))

        try:
            basket = Basket.objects.get(pk=basket_id)
        except Basket.DoesNotExist:
            return redirect(self.payment_processor.error_url)
        else:
            basket.strategy = strategy.Default()

        logger.info('********************************************************************************************')
        logger.info('---------------basket = {}'.format(basket))
        logger.info('---------------basket.total_excl_tax = {}'.format(basket.total_excl_tax))
        logger.info('********************************************************************************************')

        receipt_url = get_receipt_page_url(
            order_number=basket.order_number,
            site_configuration=basket.site.siteconfiguration,
            disable_back_button=True,
        )
        logger.info('---------------01')
        try:
            with transaction.atomic():
                try:
                    logger.info('---------------02')
                    self.handle_payment(iyzico_response, basket)
                except PaymentError:
                    return redirect(self.payment_processor.error_url)
        except:  # pylint: disable=bare-except
            logger.info('---------------03')
            logger.exception('Attempts to handle payment for basket [%d] failed.', basket.id)
            return redirect(receipt_url)

        try:
            logger.info('---------------04')
            order = self.create_order(request, basket)
            logger.info('---------------05')
        except Exception:  # pylint: disable=broad-except
            # any errors here will be logged in the create_order method. If we wanted any
            # Iyzico specific logging for this error, we would do that here.
            logger.info('---------------06')
            return redirect(receipt_url)

        try:
            logger.info('---------------07')
            self.handle_post_order(order)
            logger.info('---------------08')
        except Exception:  # pylint: disable=broad-except
            logger.info('---------------09')
            self.log_order_placement_exception(basket.order_number, basket.id)

        logger.info('---------------10')
        return redirect(receipt_url)



# class xxxIyzicoPaymentExecutionView(EdxOrderPlacementMixin, View):
#     """Execute an approved Iyzico payment and place an order for paid products as appropriate."""
#
#     @property
#     def payment_processor(self):
#         logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
#         return Iyzico(self.request.site)
#
#     # Disable atomicity for the view. Otherwise, we'd be unable to commit to the database
#     # until the request had concluded; Django will refuse to commit when an atomic() block
#     # is active, since that would break atomicity. Without an order present in the database
#     # at the time fulfillment is attempted, asynchronous order fulfillment tasks will fail.
#     @method_decorator(transaction.non_atomic_requests)
#     def dispatch(self, request, *args, **kwargs):
#         logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
#         return super(IyzicoPaymentExecutionView, self).dispatch(request, *args, **kwargs)
#
#     def _add_dynamic_discount_to_request(self, basket):
#         # TODO: Remove as a part of REVMI-124 as this is a hacky solution
#         # The problem is that orders are being created after payment processing, and the discount is not
#         # saved in the database, so it needs to be calculated again in order to save the correct info to the
#         # order. REVMI-124 will create the order before payment processing, when we have the discount context.
#         logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
#         if waffle.flag_is_active(self.request, DYNAMIC_DISCOUNT_FLAG) and basket.lines.count() == 1:
#             discount_lms_url = get_lms_url('/api/discounts/')
#             lms_discount_client = EdxRestApiClient(discount_lms_url,
#                                                    jwt=self.request.site.siteconfiguration.access_token)
#             ck = basket.lines.first().product.course_id
#             user_id = basket.owner.lms_user_id
#             try:
#                 response = lms_discount_client.user(user_id).course(ck).get()
#                 self.request.GET = self.request.GET.copy()
#                 self.request.GET['discount_jwt'] = response.get('jwt')
#             except (SlumberHttpBaseException, Timeout) as error:
#                 logger.warning(
#                     'Failed to get discount jwt from LMS. [%s] returned [%s]',
#                     discount_lms_url,
#                     error.response)
#             # END TODO
#
#     def _get_basket(self, payment_id):
#         """
#         Retrieve a basket using a payment ID.
#
#         Arguments:
#             payment_id: payment_id received from Iyzico.
#
#         Returns:
#             It will return related basket or log exception and return None if
#             duplicate payment_id received or any other exception occurred.
#
#         """
#         logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
#         try:
#             basket = PaymentProcessorResponse.objects.get(
#                 processor_name=self.payment_processor.NAME,
#                 transaction_id=payment_id
#             ).basket
#             basket.strategy = strategy.Default()
#
#             # TODO: Remove as a part of REVMI-124 as this is a hacky solution
#             # The problem is that orders are being created after payment processing, and the discount is not
#             # saved in the database, so it needs to be calculated again in order to save the correct info to the
#             # order. REVMI-124 will create the order before payment processing, when we have the discount context.
#             self._add_dynamic_discount_to_request(basket)
#             # END TODO
#
#             Applicator().apply(basket, basket.owner, self.request)
#
#             basket_add_organization_attribute(basket, self.request.GET)
#             return basket
#         except MultipleObjectsReturned:
#             logger.warning(u"Duplicate payment ID [%s] received from Iyzico.", payment_id)
#             return None
#         except Exception:  # pylint: disable=broad-except
#             logger.exception(u"Unexpected error during basket retrieval while executing Iyzico payment.")
#             return None
#
#     def get(self, request):
#         logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
#         self._get_post(request)
#     def post(self, request):
#         logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
#         self._get_post(request)
#     def _get_post(self, request):
#         """Handle an incoming user returned to us by Iyzico after approving payment."""
#         logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
#         logger.info('---------------token = {}'.format(request.GET.get('token', 'nothing')))
#         logger.info('---------------token = {}'.format(request.POST.get('token', 'nothing')))
#         payment_id = request.GET.get('paymentId')
#         payer_id = request.GET.get('PayerID')
#         logger.info(u"Payment [%s] approved by payer [%s]", payment_id, payer_id)
#
#         iyzico_response = request.GET.dict()
#         basket = self._get_basket(payment_id)
#
#         if not basket:
#             return redirect(self.payment_processor.error_url)
#
#         receipt_url = get_receipt_page_url(
#             order_number=basket.order_number,
#             site_configuration=basket.site.siteconfiguration,
#             disable_back_button=True,
#         )
#         logger.info('---------------receipt_url = {}'.format(receipt_url))
#         logger.info('---------------+++++++++++++++01')
#
#         try:
#             with transaction.atomic():
#                 try:
#                     logger.info('---------------+++++++++++++++02')
#                     self.handle_payment(iyzico_response, basket)
#                     logger.info('---------------+++++++++++++++03')
#                 except PaymentError:
#                     logger.info('---------------+++++++++++++++04')
#                     return redirect(self.payment_processor.error_url)
#         except:  # pylint: disable=bare-except
#             logger.info('---------------+++++++++++++++05')
#             logger.exception('Attempts to handle payment for basket [%d] failed.', basket.id)
#             return redirect(receipt_url)
#
#         try:
#             logger.info('---------------+++++++++++++++06')
#             order = self.create_order(request, basket)
#             logger.info('---------------+++++++++++++++07')
#         except Exception:  # pylint: disable=broad-except
#             # any errors here will be logged in the create_order method. If we wanted any
#             # Iyzico specific logging for this error, we would do that here.
#             logger.info('---------------+++++++++++++++08')
#             return redirect(receipt_url)
#
#         try:
#             logger.info('---------------+++++++++++++++09')
#             self.handle_post_order(order)
#             logger.info('---------------+++++++++++++++10')
#         except Exception:  # pylint: disable=broad-except
#             logger.info('---------------+++++++++++++++11')
#             self.log_order_placement_exception(basket.order_number, basket.id)
#
#         logger.info('---------------+++++++++++++++12')
#         return redirect(receipt_url)
#
#
# class IyzicoProfileAdminView(View):
#     ACTIONS = ('list', 'create', 'show', 'update', 'delete', 'enable', 'disable')
#
#     def dispatch(self, request, *args, **kwargs):
#         logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
#         if not request.user.is_superuser:
#             raise Http404
#
#         return super(IyzicoProfileAdminView, self).dispatch(request, *args, **kwargs)
#
#     def get(self, request, *_args, **_kwargs):
#         logger.info('---------------{}.{}'.format(type(self).__name__, inspect.stack()[0][3]))
#
#         # Capture all output and logging
#         out = StringIO()
#         err = StringIO()
#         log = StringIO()
#
#         log_handler = logging.StreamHandler(log)
#         formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
#         log_handler.setFormatter(formatter)
#         logger.addHandler(log_handler)
#
#         action = request.GET.get('action')
#         if action not in self.ACTIONS:
#             return HttpResponseBadRequest("Invalid action.")
#         profile_id = request.GET.get('id', '')
#         json_str = request.GET.get('json', '')
#
#         command_params = [action]
#         if action in ('show', 'update', 'delete', 'enable', 'disable'):
#             command_params.append(profile_id.strip())
#         if action in ('create', 'update'):
#             command_params.append(json_str.strip())
#
#         logger.info("user %s is managing iyzico profiles: %s", request.user.username, command_params)
#
#         success = False
#         try:
#             call_command('iyzico_profile', *command_params,
#                          settings=os.environ['DJANGO_SETTINGS_MODULE'], stdout=out, stderr=err)
#             success = True
#         except:  # pylint: disable=bare-except
#             # we still want to present the output whether or not the command succeeded.
#             pass
#
#         # Format the output for display
#         output = u'STDOUT\n{out}\n\nSTDERR\n{err}\n\nLOG\n{log}'.format(out=out.getvalue(), err=err.getvalue(),
#                                                                         log=log.getvalue())
#
#         # Remove the log capture handler
#         logger.removeHandler(log_handler)
#
#         return HttpResponse(output, content_type='text/plain', status=200 if success else 500)
