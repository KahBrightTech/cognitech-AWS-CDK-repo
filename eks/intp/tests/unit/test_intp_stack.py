import aws_cdk as core
import aws_cdk.assertions as assertions

from intp.intp_stack import IntpStack

# example tests. To run these tests, uncomment this file along with the example
# resource in intp/intp_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = IntpStack(app, "intp")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
