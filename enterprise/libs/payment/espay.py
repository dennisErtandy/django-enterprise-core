from enterprise.structures.transaction.models.espay import Espay
import requests
import hashlib
import json
from django.conf import settings
from uuid import uuid4
from datetime import datetime
from django.core.serializers.json import DjangoJSONEncoder
from rest_framework import mixins, viewsets, status, serializers, permissions
from rest_framework.response import Response


SIGNATURE_KEY = getattr(settings, 'ESPAY_SIGNATURE_KEY', '')
API_KEY = getattr(settings, 'ESPAY_API_KEY', '')
ESPAY_COMMERCE_CODE = getattr(settings, 'ESPAY_COMMERCE_CODE', '')
ESPAY_PASSWORD = getattr(settings, 'ESPAY_PASSWORD', '')


class _BaseEspay(object):
    '''
    Abstract class for payment with espay
    '''
    payload = {}
    invoice = None
    request_status_code = None
    request = None

    def __init__(self, invoice):
        self.invoice = invoice

    def get_order_id(self, espay_id62):
        order_id = self.invoice.number+'-'+espay_id62
        if not getattr(settings, 'PRODUCTION', False):
            order_id = '%s-%s-%s' % (
                order_id.split('-')[0],
                datetime.timestamp(datetime.now()),
                order_id.split('-')[1]
            )
        return order_id

    def add_payload(self, *args, **kwargs):
        self.payload = {**self.payload, **kwargs}

    def post_request(self, url):
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
        }
        r = requests.post(url, data=self.payload, headers=headers)
        r_dict = r.json()
        self.request = r
        self.request_status_code = r_dict.get('status_code')

        return r_dict

    def get_signature(self, bare_signature):
        upper_signature = bare_signature.upper()
        signature = hashlib.sha256(upper_signature.encode()).hexdigest()
        return signature

    def get_curl(self):
        if not self.request:
            return '-'
        req = self.request.request
        command = "curl -X {method} -H {headers} -d '{data}' '{uri}'"
        method = req.method
        uri = req.url
        data = req.body
        req_headers = ['"{0}: {1}"'.format(k, v) for k, v in req.headers.items()]
        req_headers = " -H ".join(req_headers)
        return command.format(method=method, headers=req_headers, data=data, uri=uri)


class EspayPG(_BaseEspay):

    def get_send_invoice_url(self):
        return '%smerchantpg/sendinvoice' % getattr(settings, 'ESPAY_API_URL',
                                                   'https://sandbox-api.espay.id/rest/')

    def send_invoice(self, bank_code):
        from enterprise.structures.transaction.models.espay import Espay

        invoice = self.invoice
        invoice.published_at = datetime.now()
        invoice.save()
        user = invoice.owned_by

        if not self.invoice:
            raise Exception('Invoice is null')

        amount = invoice.amount
        espay = Espay(
            payment_type=bank_code,
            transaction_status='preparing',
            amount=amount,
            payload=self.payload
        )
        espay.created_by = user
        espay.save()

        rq_uuid = uuid4()
        rq_datetime = datetime.now()
        order_id = self.get_order_id(espay.id62)
        ccy = getattr(settings, 'ESPAY_CCY', 'IDR')
        comm_code = ESPAY_COMMERCE_CODE
        remark1 = user.phone_number
        remark2 = user.full_name
        remark3 = user.email
        update = 'N'
        va_expired = getattr(settings, 'ESPAY_VA_EXPIRED_MINUTE', '120')
        bare_signature = '##%s##%s##%s##%s##%s##%s##%s##SENDINVOICE##' % (
            SIGNATURE_KEY,
            rq_uuid,
            rq_datetime,
            order_id,
            amount,
            ccy,
            comm_code,
        )
        signature = self.get_signature(bare_signature)

        self.add_payload(rq_uuid=str(rq_uuid))
        self.add_payload(rq_datetime=str(rq_datetime))
        self.add_payload(order_id=order_id)
        self.add_payload(amount=str(amount))
        self.add_payload(ccy=ccy)
        self.add_payload(comm_code=comm_code)
        self.add_payload(remark1=remark1)
        self.add_payload(remark2=remark2)
        self.add_payload(remark3=remark3)
        self.add_payload(bank_code=bank_code)
        self.add_payload(update=update)
        self.add_payload(va_expired=va_expired)
        self.add_payload(signature=signature)
        self.add_payload(bare_signature=bare_signature)

        espay.transaction_id = order_id
        espay.payload = self.payload
        espay.save()

        result = self.post_request(self.get_send_invoice_url())
        espay.responses = result
        espay.save()

        return result


class BankView(viewsets.GenericViewSet, mixins.ListModelMixin):

    def get_serializer(self, *args, **kwargs):
        return None

    def list(self, request):
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
        }
        url = '%smerchant/merchantinfo' % getattr(settings, 'ESPAY_API_URL',
                                                   'https://sandbox-api.espay.id/rest/')
        r = requests.post(url, data={'key': API_KEY}, headers=headers)
        resp = r.json()

        if not 'error_code' in resp:
            return Response(
                {'error_message': r.text},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if resp.get('error_code') != '0000':
            return Response(
                {'error_message': resp.get('error_message')},
                status=status.HTTP_400_BAD_REQUEST
            )
        data = resp.get('data')
        return Response(
            resp.get('data'),
            status=status.HTTP_200_OK
        )

class InquirySerializer(serializers.Serializer):
    rq_uuid = serializers.CharField()
    password = serializers.CharField(required=False)
    signature = serializers.CharField()
    comm_code = serializers.CharField(required=False)
    order_id = serializers.CharField()
    error_message = serializers.CharField(required=False)
    error_code = serializers.CharField(required=False)
    rs_datetime = serializers.DateTimeField(required=False)
    amount = serializers.DecimalField(required=False, max_digits=19, decimal_places=2)
    ccy = serializers.CharField(required=False)
    description = serializers.CharField(required=False)
    trx_date = serializers.DateTimeField(required=False)

    def validate(self, attrs):
        validated_data= super().validate(attrs)
        password = validated_data.get('password')
        comm_code = validated_data.get('comm_code')
        order_id = validated_data.get('order_id')

        if password != ESPAY_PASSWORD or comm_code != ESPAY_COMMERCE_CODE:
            validated_data['error_message'] = 'Invalid credentials'
            validated_data['error_code'] = '0403'

        espay = Espay.objects.filter(
            transaction_id = order_id
        )
        payload = espay.payload

        if not espay:
            validated_data['error_message'] = 'Order not found'
            validated_data['error_code'] = '0404'

        validated_data['error_message'] = 'Success'
        validated_data['error_code'] = '0000'
        validated_data['rs_datetime'] = datetime.now()
        validated_data['amount'] = espay.amount
        validated_data['ccy'] = payload.ccy
        validated_data['description'] = 'Payment for: %s' % order_id
        validated_data['trx_date'] = espay.created_at

        validated_data.pop('password')
        validated_data.pop('comm_code')
        salt_string = 'INQUIRY-RS'

        ##Signature Key##rq_uuid##rs_datetime##order_id##error_code##INQUIRY-RS##
        bare_signature = '##%s##%s##%s##%s##%s##%s##' % (
            SIGNATURE_KEY, 
            validated_data.get('rq_uuid'), 
            validated_data.get('rs_datetime'),
            order_id,
            validated_data.get('error_code'),
            salt_string
        )
        upper_signature = bare_signature.upper()
        signature = hashlib.sha256(upper_signature.encode()).hexdigest()

        validated_data['signature'] = signature

        payload['rs_signature'] = signature
        payload['rs_bare_signature'] = bare_signature
        espay.payload = payload
        espay.save()

        return validated_data

class InquiryView(viewsets.GenericViewSet, mixins.CreateModelMixin):
    serializer_class = InquirySerializer
    permission_classes = (permissions.AllowAny,)
    
    def perform_create(self, serializer):
        pass

