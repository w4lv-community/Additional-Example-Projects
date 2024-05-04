Sending errors to a Caller (or to a Worker further up the Worker call-chain)
It is quite common when developing asynchronous applications with Workers to want to send the errors that occur in one Worker up to another Worker in the Worker call-chain to handle the error. However, it is not always known which Worker in the Worker call-chain hierarchy will handle the errors of Workers below it. Maybe a Worker's Caller will handle the error of a subWorker, or it might be that the head Worker of an application is used to collect all the errors from all Workers below it. Depending on the design of your application and the level of coupling you want to have between your Workers, there are a few ways to handle this problem.

Method 1: Using a Public API Response
Advantages: Simple, decoupled from Caller, uses Worker Public API, does not require a base class.

Disadvantages: If sending the error multiple levels up the Worker call-chain, you will need to implement a similar Public API Response in every Worker between the Worker that sends the error and the Worker that processes it.

To create a Public API Response, you can use the Public API Builder tool to create a Public Response with its data input type as a standard LabVIEW error cluster. You can then use the Response VI to send an error to the Worker's Caller from anywhere within the Worker's MHL.

To learn how to create Worker Public API Responses, see the link below.


Method 2: Using the Workers Palette "Send Error to Owner" VI
Advantages: Simple, decoupled from Caller, does not require a base class. Requires no additional VIs.

Disadvantages: Not part of a Worker's Public API so it might not be obvious to other developers that you have used this method.

This VI can be found on the Workers Palette. The VI will pass input Error to Pass to Owner to the Worker's Caller. The Caller will then continue to pass the error up to its Caller, all the way up to the head Worker.


Pass Error to Owner.vi can be found on the Workers Palette

The error can be captured by any Worker in the application's Worker call-chain above the Worker that calls
