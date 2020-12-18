""" Iyzico payment processing. """
from __future__ import absolute_import, unicode_literals

import logging
import re
import uuid
from decimal import Decimal

import paypalrestsdk
import six  # pylint: disable=ungrouped-imports
import waffle
from django.conf import settings
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.translation import get_language
from oscar.apps.payment.exceptions import GatewayError
from six.moves import range
from six.moves.urllib.parse import urljoin

from ecommerce.core.url_utils import get_ecommerce_url
from ecommerce.extensions.payment.constants import PAYPAL_LOCALES
from ecommerce.extensions.payment.models import IyzicoProcessorConfiguration, IyzicoWebProfile
from ecommerce.extensions.payment.processors import BasePaymentProcessor, HandledProcessorResponse
from ecommerce.extensions.payment.utils import get_basket_program_uuid, middle_truncate

logger = logging.getLogger(__name__)


class Iyzico(BasePaymentProcessor):
    """
    Iyzico REST API (May 2015)

    For reference, see https://developer.iyzico.com/docs/api/.
    """

    NAME = 'iyzico'
    TITLE = 'Iyzico'
    DEFAULT_PROFILE_NAME = 'default'

    def __init__(self, site):
        """
        Constructs a new instance of the Iyzico processor.

        Raises:
            KeyError: If a required setting is not configured for this payment processor
        """
        super(Iyzico, self).__init__(site)

        # Number of times payment execution is retried after failure.
        self.retry_attempts = IyzicoProcessorConfiguration.get_solo().retry_attempts

    @cached_property
    def iyzico_api(self):
        """
        Returns Iyzico API instance with appropriate configuration
        Returns: Iyzico API instance
        """
        return paypalrestsdk.Api({
            'mode': self.configuration['mode'],
            'client_id': self.configuration['client_id'],
            'client_secret': self.configuration['client_secret']
        })

    @property
    def cancel_url(self):
        return get_ecommerce_url(self.configuration['cancel_checkout_path'])

    @property
    def error_url(self):
        return get_ecommerce_url(self.configuration['error_path'])

    def resolve_iyzico_locale(self, language_code):
        default_iyzico_locale = PAYPAL_LOCALES.get(re.split(r'[_-]', get_language())[0].lower())
        if not language_code:
            return default_iyzico_locale

        return PAYPAL_LOCALES.get(re.split(r'[_-]', language_code)[0].lower(), default_iyzico_locale)

    def create_temporary_web_profile(self, locale_code):
        """
        Generates a temporary Iyzico WebProfile that carries the locale setting for a Iyzico Payment
        and returns the id of the WebProfile
        """
        try:
            web_profile = paypalrestsdk.WebProfile({
                "name": str(uuid.uuid1()),  # Generate a unique identifier
                "presentation": {
                    "locale_code": locale_code
                },
                "temporary": True  # Persists for 3 hours
            }, api=self.iyzico_api)

            if web_profile.create():
                msg = "Web Profile[%s] for locale %s created successfully" % (
                    web_profile.id,
                    web_profile.presentation.locale_code
                )
                logger.info(msg)
                return web_profile.id

            msg = "Web profile creation encountered error [%s]. Will continue without one" % (
                web_profile.error
            )
            logger.warning(msg)
            return None

        except Exception:  # pylint: disable=broad-except
            logger.warning("Creating Iyzico WebProfile resulted in exception. Will continue without one.")
            return None

    def get_courseid_title(self, line):
        """
        Get CourseID & Title from basket item

        Arguments:
            line: basket item

        Returns:
             Concatenated string containing course id & title if exists.
        """
        courseid = ''
        line_course = line.product.course
        if line_course:
            courseid = "{}|".format(line_course.id)
        return courseid + line.product.title

    def get_transaction_parameters(self, basket, request=None, use_client_side_checkout=False, **kwargs):
        """
        Create a new Iyzico payment.

        Arguments:
            basket (Basket): The basket of products being purchased.
            request (Request, optional): A Request object which is used to construct Iyzico's `return_url`.
            use_client_side_checkout (bool, optional): This value is not used.
            **kwargs: Additional parameters; not used by this method.

        Returns:
            dict: Iyzico-specific parameters required to complete a transaction. Must contain a URL
                to which users can be directed in order to approve a newly created payment.

        Raises:
            GatewayError: Indicates a general error or unexpected behavior on the part of Iyzico which prevented
                a payment from being created.
        """
        # Iyzico requires that item names be at most 127 characters long.
        PAYPAL_FREE_FORM_FIELD_MAX_SIZE = 127
        return_url = urljoin(get_ecommerce_url(), reverse('iyzico:execute'))
        data = {
            'intent': 'sale',
            'redirect_urls': {
                'return_url': return_url,
                'cancel_url': self.cancel_url,
            },
            'payer': {
                'payment_method': 'iyzico',
            },
            'transactions': [{
                'amount': {
                    'total': six.text_type(basket.total_incl_tax),
                    'currency': basket.currency,
                },
                # Iyzico allows us to send additional transaction related data in 'description' & 'custom' field
                # Free form field, max length 127 characters
                # description : program_id:<program_id>
                'description': "program_id:{}".format(get_basket_program_uuid(basket)),
                'item_list': {
                    'items': [
                        {
                            'quantity': line.quantity,
                            # Iyzico requires that item names be at most 127 characters long.
                            # for courseid we're using 'name' field along with title,
                            # concatenated field will be 'courseid|title'
                            'name': middle_truncate(self.get_courseid_title(line), PAYPAL_FREE_FORM_FIELD_MAX_SIZE),
                            # Iyzico requires that the sum of all the item prices (where price = price * quantity)
                            # equals to the total amount set in amount['total'].
                            'price': six.text_type(line.line_price_incl_tax_incl_discounts / line.quantity),
                            'currency': line.stockrecord.price_currency,
                        }
                        for line in basket.all_lines()
                    ],
                },
                'invoice_number': basket.order_number,
            }],
        }

        if waffle.switch_is_active('create_and_set_webprofile'):
            locale_code = self.resolve_iyzico_locale(request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME))
            web_profile_id = self.create_temporary_web_profile(locale_code)
            if web_profile_id is not None:
                data['experience_profile_id'] = web_profile_id
        else:
            try:
                web_profile = IyzicoWebProfile.objects.get(name=self.DEFAULT_PROFILE_NAME)
                data['experience_profile_id'] = web_profile.id
            except IyzicoWebProfile.DoesNotExist:
                pass

        available_attempts = 1
        if waffle.switch_is_active('PAYPAL_RETRY_ATTEMPTS'):
            available_attempts = self.retry_attempts

        for i in range(1, available_attempts + 1):
            try:
                payment = paypalrestsdk.Payment(data, api=self.iyzico_api)
                payment.create()
                if payment.success():
                    break
                if i < available_attempts:
                    logger.warning(
                        u"Creating Iyzico payment for basket [%d] was unsuccessful. Will retry.",
                        basket.id,
                        exc_info=True
                    )
                else:
                    error = self._get_error(payment)
                    # pylint: disable=unsubscriptable-object
                    entry = self.record_processor_response(
                        error,
                        transaction_id=error['debug_id'],
                        basket=basket
                    )
                    logger.error(
                        u"%s [%d], %s [%d].",
                        "Failed to create Iyzico payment for basket",
                        basket.id,
                        "Iyzico's response recorded in entry",
                        entry.id,
                        exc_info=True
                    )
                    raise GatewayError(error)

            except:  # pylint: disable=bare-except
                if i < available_attempts:
                    logger.warning(
                        u"Creating Iyzico payment for basket [%d] resulted in an exception. Will retry.",
                        basket.id,
                        exc_info=True
                    )
                else:
                    logger.exception(
                        u"After %d retries, creating Iyzico payment for basket [%d] still experienced exception.",
                        i,
                        basket.id
                    )
                    raise

        entry = self.record_processor_response(payment.to_dict(), transaction_id=payment.id, basket=basket)
        logger.info("Successfully created Iyzico payment [%s] for basket [%d].", payment.id, basket.id)

        for link in payment.links:
            if link.rel == 'approval_url':
                approval_url = link.href
                break
        else:
            logger.error(
                "Approval URL missing from Iyzico payment [%s]. Iyzico's response was recorded in entry [%d].",
                payment.id,
                entry.id
            )
            raise GatewayError(
                'Approval URL missing from Iyzico payment response. See entry [{}] for details.'.format(entry.id))

        parameters = {
            'payment_page_url': approval_url,
        }

        return parameters

    def handle_processor_response(self, response, basket=None):
        """
        Execute an approved Iyzico payment.

        This method creates PaymentEvents and Sources for approved payments.

        Arguments:
            response (dict): Dictionary of parameters returned by Iyzico in the `return_url` query string.

        Keyword Arguments:
            basket (Basket): Basket being purchased via the payment processor.

        Raises:
            GatewayError: Indicates a general error or unexpected behavior on the part of Iyzico which prevented
                an approved payment from being executed.

        Returns:
            HandledProcessorResponse
        """
        data = {'payer_id': response.get('PayerID')}

        # By default Iyzico payment will be executed only once.
        available_attempts = 1

        # Add retry attempts (provided in the configuration)
        # if the waffle switch 'ENABLE_PAYPAL_RETRY' is set
        if waffle.switch_is_active('PAYPAL_RETRY_ATTEMPTS'):
            available_attempts = available_attempts + self.retry_attempts

        for attempt_count in range(1, available_attempts + 1):
            payment = paypalrestsdk.Payment.find(response.get('paymentId'), api=self.iyzico_api)
            payment.execute(data)

            if payment.success():
                # On success break the loop.
                break

            # Raise an exception for payments that were not successfully executed. Consuming code is
            # responsible for handling the exception
            error = self._get_error(payment)
            # pylint: disable=unsubscriptable-object
            entry = self.record_processor_response(error, transaction_id=error['debug_id'], basket=basket)

            logger.warning(
                "Failed to execute Iyzico payment on attempt [%d]. "
                "Iyzico's response was recorded in entry [%d].",
                attempt_count,
                entry.id
            )

            # After utilizing all retry attempts, raise the exception 'GatewayError'
            if attempt_count == available_attempts:
                logger.error(
                    "Failed to execute Iyzico payment [%s]. "
                    "Iyzico's response was recorded in entry [%d].",
                    payment.id,
                    entry.id
                )
                raise GatewayError

        self.record_processor_response(payment.to_dict(), transaction_id=payment.id, basket=basket)
        logger.info("Successfully executed Iyzico payment [%s] for basket [%d].", payment.id, basket.id)

        currency = payment.transactions[0].amount.currency
        total = Decimal(payment.transactions[0].amount.total)
        transaction_id = payment.id
        # payer_info.email may be None, see:
        # http://stackoverflow.com/questions/24090460/iyzico-rest-api-return-empty-payer-info-for-non-us-accounts
        email = payment.payer.payer_info.email
        label = 'Iyzico ({})'.format(email) if email else 'Iyzico Account'

        return HandledProcessorResponse(
            transaction_id=transaction_id,
            total=total,
            currency=currency,
            card_number=label,
            card_type=None
        )

    def _get_error(self, payment):
        """
        Shameful workaround for mocking the `error` attribute on instances of
        `paypalrestsdk.Payment`. The `error` attribute is created at runtime,
        but passing `create=True` to `patch()` isn't enough to mock the
        attribute in this module.
        """
        return payment.error  # pragma: no cover

    def _get_payment_sale(self, payment):
        """
        Returns the Sale related to a given Payment.

        Note (CCB): We mostly expect to have a single sale and transaction per payment. If we
        ever move to a split payment scenario, this will need to be updated.
        """
        for transaction in payment.transactions:
            for related_resource in transaction.related_resources:
                try:
                    return related_resource.sale
                except Exception:  # pylint: disable=broad-except
                    continue

        return None

    def issue_credit(self, order_number, basket, reference_number, amount, currency):
        try:
            payment = paypalrestsdk.Payment.find(reference_number, api=self.iyzico_api)
            sale = self._get_payment_sale(payment)

            if not sale:
                logger.error('Unable to find a Sale associated with Iyzico Payment [%s].', payment.id)

            refund = sale.refund({
                'amount': {
                    'total': six.text_type(amount),
                    'currency': currency,
                }
            })

        except:
            msg = 'An error occurred while attempting to issue a credit (via Iyzico) for order [{}].'.format(
                order_number)
            logger.exception(msg)
            raise GatewayError(msg)

        if refund.success():
            transaction_id = refund.id
            self.record_processor_response(refund.to_dict(), transaction_id=transaction_id, basket=basket)
            return transaction_id

        error = refund.error
        entry = self.record_processor_response(error, transaction_id=error['debug_id'], basket=basket)

        msg = "Failed to refund Iyzico payment [{sale_id}]. " \
              "Iyzico's response was recorded in entry [{response_id}].".format(sale_id=sale.id,
                                                                                response_id=entry.id)
        raise GatewayError(msg)
