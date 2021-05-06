import logging
import sys
import datetime
from enum import Enum
from typing import List, Dict, Any

from googleapiclient.discovery import build

from googleapiwrapper.gmail_api_extensions import CachingStrategyType, CachingStrategy, ApiFetchingContext
from googleapiwrapper.gmail_domain import ApiItemType, Message, MessagePartDescriptor, MessagePart, \
    GmailMessageBodyPart, ThreadsResponseField, MessagePartField, MessagePartBodyField, HeaderField, MessagePartBody, \
    Header, ThreadField, GetAttachmentParam, MessageField, Thread, ListQueryParam, GmailThreads, GenericObjectHelper as GH
from googleapiwrapper.google_auth import GoogleApiAuthorizer, AuthedSession
from pythoncommons.string_utils import auto_str
LOG = logging.getLogger(__name__)


class Progress:
    def __init__(self, item_type: ApiItemType, limit: int = None):
        self.requests_count = 0
        self.all_items_count = 0
        self.processed_items = 0
        self.new_items_with_last_request = -1
        self.item_type = item_type
        self.limit = limit

    def _print_status(self):
        LOG.info(f"[Request #: {self.requests_count}] "
                 f"Received {self.new_items_with_last_request} more {self.item_type.value}s")

    def incr_requests(self):
        self.requests_count += 1

    def register_new_items(self, number_of_new_items: int, print_status=True):
        self.all_items_count += number_of_new_items
        self.new_items_with_last_request = number_of_new_items
        if print_status:
            self._print_status()

    def incr_processed_items(self):
        self.processed_items += 1

    def is_limit_reached(self):
        if self.limit:
            return self.processed_items > self.limit
        return False

    def print_processing_items(self):
        LOG.debug(f"Processing {self.item_type.value}s: {self.processed_items} / {self.all_items_count}")


@auto_str
class ApiConversionContext:
    def __init__(self, item_type: ApiItemType, limit: int = None):
        self.progress = Progress(item_type, limit=limit)
        self.decode_errors: List[MessagePartDescriptor] = []
        self.empty_bodies: List[MessagePartDescriptor] = []

        # Set later
        self.current_message: Message or None = None
        self.current_message_part: MessagePart or None = None

    def register_current_message(self, message: Message):
        self.current_message: Message = message

    def register_current_message_part(self, message_part: MessagePart):
        self.current_message_part = message_part

    def report_decode_error(self, gmail_msg_body_part: GmailMessageBodyPart):
        # TODO error log
        self.decode_errors.append(MessagePartDescriptor(self.current_message,
                                                        self.current_message_part, gmail_msg_body_part))

    def report_empty_body(self, gmail_msg_body_part: GmailMessageBodyPart):
        # TODO error log
        self.empty_bodies.append(MessagePartDescriptor(self.current_message,
                                                       self.current_message_part, gmail_msg_body_part))

    def handle_encoding_errors(self):
        # TODO error log all
        self.decode_errors.clear()

    def handle_empty_bodies(self, func):
        # TODO error log all
        for descriptor in self.empty_bodies:
            func(descriptor)
        self.empty_bodies.clear()


CONVERSION_CONTEXT: ApiConversionContext = None
module = sys.modules[__name__]


class GmailWrapper:
    USERID_ME = 'me'
    DEFAULT_API_FIELDS = {ListQueryParam.USER_ID.value: USERID_ME}
    DEFAULT_PAGE_SIZE = 100

    def __init__(self, authorizer: GoogleApiAuthorizer,
                 api_version: str = None,
                 cache_strategy_type: CachingStrategyType = CachingStrategyType.FILESYSTEM_CACHE_STRATEGY,
                 output_basedir: str = None):
        self.authed_session: AuthedSession = authorizer.authorize()
        cache_strategy_obj = cache_strategy_type.value(output_basedir,
                                                       self.authed_session.project_name,
                                                       self.authed_session.user_email)
        self.api_fetching_ctx: ApiFetchingContext = ApiFetchingContext(cache_strategy_obj)
        if not api_version:
            api_version = authorizer.service_type.default_api_version
        self.service = build(authorizer.service_type.service_name, api_version,
                             credentials=self.authed_session.authed_creds)
        self.users_svc = self.service.users()
        self.messages_svc = self.users_svc.messages()
        self.threads_svc = self.users_svc.threads()
        self.attachments_svc = self.messages_svc.attachments()

    def query_threads_with_paging(self,
                                  query: str = None,
                                  limit: int = None,
                                  sanity_check=True,
                                  expect_one_message_per_thread=False) -> GmailThreads:
        module.CONVERSION_CONTEXT = ApiConversionContext(ApiItemType.THREAD, limit=limit)
        ctx = CONVERSION_CONTEXT
        kwargs = self._get_new_kwargs()
        if query:
            kwargs[ListQueryParam.QUERY.value] = query
        if limit and limit < GmailWrapper.DEFAULT_PAGE_SIZE:
            kwargs[ListQueryParam.MAX_RESULTS.value] = limit
        request = self.threads_svc.list(**kwargs)

        threads = GmailThreads()
        while request is not None:
            response: Dict[str, Any] = request.execute()
            if response:
                ctx.progress.incr_requests()
                list_of_threads: List[Dict[str, str]] = response.get(ThreadsResponseField.THREADS.value, [])
                ctx.progress.register_new_items(len(list_of_threads), print_status=True)

                thread_ids: List[str] = [GH.get_field(t, ThreadField.ID) for t in list_of_threads]
                thread_ids_to_query = self.api_fetching_ctx.get_thread_ids_to_query_from_api(thread_ids,
                                                                                             expect_one_message_per_thread=expect_one_message_per_thread)
                if thread_ids_to_query:
                    LOG.debug(f"API fetching context returned email thread IDs to query again: {thread_ids_to_query}")
                else:
                    LOG.debug(f"API fetching context returned no email thread IDs to query in this round")

                for idx, thread_id in enumerate(thread_ids_to_query):
                    ctx.progress.incr_processed_items()
                    if ctx.progress.is_limit_reached():
                        LOG.warning(f"Reached request limit of {limit}, stop processing more items.")
                        return threads
                    ctx.progress.print_processing_items()

                    thread_response: Dict[str, Any] = self._query_thread_data(thread_id)
                    messages_response: List[Dict[str, Any]] = GH.get_field(thread_response, ThreadField.MESSAGES)
                    messages: List[Message] = [self.parse_api_message(message) for message in messages_response]
                    ctx.handle_empty_bodies(lambda desc: self._query_attachment_of_descriptor(desc))
                    thread_obj: Thread = Thread(thread_id, messages[0].subject, messages)
                    # Add Thread object. This action will internally create GmailMessage and rest of the stuff
                    threads.add(thread_obj)
                    if sanity_check:
                        self._sanity_check(thread_obj)
                    self.api_fetching_ctx.process_thread(thread_response, thread_obj)
            request = self.threads_svc.list_next(request, response)

        # TODO error log all messages that had missing body + attachment request
        ctx.handle_encoding_errors()
        return threads

    def _query_attachment_of_descriptor(self, descriptor: MessagePartDescriptor):
        # Fix MessagePartBody object that has attachmentId only
        # Quoting from API doc for Field 'attachmentId':
        # When present, contains the ID of an external attachment that can be retrieved in a
        # separate messages.attachments.get request.
        # When not present, the entire content of the message part body is contained in the data field.
        message_id = descriptor.message.id
        attachment_id = descriptor.message_part.body.attachmentId
        if not message_id or not attachment_id:
            LOG.error("Both message_id and attachment_id has to be set in order to query attachment details from API."
                      f"Object was: {descriptor}")
            return
        attachment_response = self._query_attachment(message_id, attachment_id)
        # TODO Implement attachment handling

    def parse_api_message(self, message: Dict):
        message_part = GH.get_field(message, MessageField.PAYLOAD)
        message_id: str = GH.get_field(message, MessageField.ID)
        message_part_obj: MessagePart = self.parse_message_part(message_part, message_id)
        return Message(
            message_id,
            GH.get_field(message, MessageField.THREAD_ID),
            datetime.datetime.fromtimestamp(int(GH.get_field(message, MessageField.DATE)) / 1000),
            GH.get_field(message, MessageField.SNIPPET),
            message_part_obj
        )

    def parse_message_part(self, message_part, message_id: str) -> MessagePart:
        message_parts = GH.get_field(message_part, MessagePartField.PARTS, [])
        headers = self._parse_headers(message_part)
        message_part_obj: MessagePart = MessagePart(
            GH.get_field(message_part, MessagePartField.PART_ID),
            GH.get_field(message_part, MessagePartField.MIME_TYPE),
            headers,
            self._parse_message_part_body_obj(GH.get_field(message_part, MessagePartField.BODY)),
            [self.parse_message_part(part, message_id) for part in message_parts],
        )
        return message_part_obj

    def _parse_headers(self, message_part):
        headers_list: List[Dict[str, str]] = GH.get_field(message_part, MessagePartField.HEADERS)
        headers: List[Header] = []
        for header_dict in headers_list:
            headers.append(Header(GH.get_field(header_dict, HeaderField.NAME),
                                  GH.get_field(header_dict, HeaderField.VALUE)))
        return headers

    def _parse_message_part_body_obj(self, messagepart_body):
        message_part_body_obj = MessagePartBody(GH.get_field(messagepart_body, MessagePartBodyField.DATA),
                                                GH.get_field(messagepart_body, MessagePartBodyField.SIZE),
                                                GH.get_field(messagepart_body, MessagePartBodyField.ATTACHMENT_ID))
        return message_part_body_obj

    def _query_thread_data(self, thread_id: str):
        kwargs = self._get_new_kwargs()
        kwargs[ThreadField.ID.value] = thread_id
        tdata = self.threads_svc.get(**kwargs).execute()
        return tdata

    def _query_attachment(self, message_id: str, attachment_id: str):
        kwargs = self._get_new_kwargs()
        kwargs[GetAttachmentParam.MESSAGE_ID.value] = message_id
        kwargs[GetAttachmentParam.ATTACHMENT_ID.value] = attachment_id
        attachment_data = self.attachments_svc.get(**kwargs).execute()
        return attachment_data

    @staticmethod
    def _get_new_kwargs():
        kwargs = {}
        kwargs.update(GmailWrapper.DEFAULT_API_FIELDS)
        return kwargs

    def _sanity_check(self, thread: Thread):
        # TODO implement checking if all messages have the same subject
        pass
